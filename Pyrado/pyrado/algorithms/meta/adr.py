# Copyright (c) 2020, Fabio Muratore, Honda Research Institute Europe GmbH, and
# Technical University of Darmstadt.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
# 3. Neither the name of Fabio Muratore, Honda Research Institute Europe GmbH,
#    or Technical University of Darmstadt, nor the names of its contributors may
#    be used to endorse or promote products derived from this software without
#    specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL FABIO MURATORE, HONDA RESEARCH INSTITUTE EUROPE GMBH,
# OR TECHNICAL UNIVERSITY OF DARMSTADT BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS;
# OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER
# IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

from typing import Dict, Optional, Sequence

import numpy as np
import torch as to
from init_args_serializer import Serializable
from torch import nn as nn
from torch.functional import Tensor
from tqdm import tqdm

import pyrado
from pyrado.algorithms.base import Algorithm
from pyrado.algorithms.step_based.svpg import SVPGBuilder, SVPGHyperparams
from pyrado.domain_randomization.domain_parameter import DomainParam
from pyrado.environment_wrappers.base import EnvWrapper
from pyrado.environment_wrappers.utils import inner_env
from pyrado.environments.base import Env
from pyrado.logger.step import StepLogger
from pyrado.policies.base import Policy
from pyrado.policies.recurrent.rnn import LSTMPolicy
from pyrado.sampling.parallel_evaluation import eval_domain_params
from pyrado.sampling.sampler_pool import SamplerPool
from pyrado.sampling.step_sequence import StepSequence
from pyrado.spaces.base import Space
from pyrado.spaces.box import BoxSpace
from pyrado.utils.data_types import EnvSpec


class ADR(Algorithm):
    """
    Active Domain Randomization (ADR)

    .. seealso::
        [1] B. Mehta, M. Diaz, F. Golemo, C.J. Pal, L. Paull, "Active Domain Randomization", arXiv, 2019
    """

    name: str = "adr"

    def __init__(
        self,
        ex_dir: pyrado.PathLike,
        env: Env,
        subrtn: Algorithm,
        adr_hp: Dict,
        svpg_hp: SVPGHyperparams,
        reward_generator_hp: Dict,
        max_iter: int,
        num_discriminator_epoch: int,
        batch_size: int,
        svpg_warmup: int = 0,
        num_workers: int = 4,
        num_trajs_per_config: int = 8,
        log_exploration: bool = False,
        randomized_params: Sequence[str] = None,
        logger: Optional[StepLogger] = None,
    ):
        """
        Constructor

        :param save_dir: directory to save the snapshots i.e. the results in
        :param env: the environment to train in
        :param subrtn: algorithm which performs the policy / value-function optimization
        :param max_iter: maximum number of iterations
        :param svpg_particle_hparam: SVPG particle hyperparameters
        :param num_svpg_particles: number of SVPG particles
        :param num_discriminator_epoch: epochs in discriminator training
        :param batch_size: batch size for training
        :param svpg_learning_rate: SVPG particle optimizers' learning rate
        :param svpg_temperature: SVPG temperature coefficient (how strong is the influence of the particles on each other)
        :param svpg_evaluation_steps: how many configurations to sample between training
        :param svpg_horizon: how many steps until the particles are reset
        :param svpg_kl_factor: kl reward coefficient
        :param svpg_warmup: number of iterations without SVPG training in the beginning
        :param svpg_serial: serial mode (see SVPG)
        :param num_workers: number of environments for parallel sampling
        :param num_trajs_per_config: number of trajectories to sample from each config
        :param max_step_length: maximum change of physics parameters per step
        :param randomized_params: which parameters to randomize
        :param logger: logger for every step of the algorithm, if `None` the default logger will be created
        """
        if not isinstance(env, Env):
            raise pyrado.TypeErr(given=env, expected_type=Env)
        if not isinstance(subrtn, Algorithm):
            raise pyrado.TypeErr(given=subrtn, expected_type=Algorithm)
        if not isinstance(subrtn.policy, Policy):
            raise pyrado.TypeErr(given=subrtn.policy, expected_type=Policy)

        # Call Algorithm's constructor
        super().__init__(ex_dir, max_iter, subrtn.policy, logger)
        self.log_loss = True

        # Store the inputs
        self.env = env
        self._subrtn = subrtn
        self._subrtn.save_name = "subrtn"
        self.num_discriminator_epoch = num_discriminator_epoch
        self.batch_size = batch_size
        self.num_trajs_per_config = num_trajs_per_config
        self.warm_up_time = svpg_warmup
        self.log_exploration = log_exploration

        self.curr_time_step = 0

        randomized_params = adr_hp["randomized_params"]

        # Get the number of params
        if isinstance(randomized_params, list) and len(randomized_params) == 0:
            randomized_params = inner_env(self.env).get_nominal_domain_param().keys()
        self.params = [DomainParam(param, 1) for param in randomized_params]
        self.num_params = len(self.params)

        # Initialize reward generator
        self.reward_generator = RewardGenerator(env.spec, logger=self.logger, **reward_generator_hp)

        # Initialize logbook
        self.sim_instances_full_horizon = np.random.random_sample(
            (
                svpg_hp["algo"]["num_particles"],
                svpg_hp["algo"]["horizon"],
                adr_hp["evaluation_steps"],
                self.num_params,
            )
        )

        # Initialize SVPG adapter

        self.svpg_wrapper = SVPGAdapter(
            env,
            self.params,
            subrtn.expl_strat,
            self.reward_generator,
            svpg_hp["algo"]["num_particles"],
            horizon=svpg_hp["algo"]["horizon"],
            num_rollouts_per_config=self.num_trajs_per_config,
            step_length=adr_hp["step_length"],
            num_workers=num_workers,
        )

        # Generate SVPG with default architecture using SVPGBuilder
        self.svpg = SVPGBuilder(ex_dir, self.svpg_wrapper, svpg_hp).svpg

    @property
    def sample_count(self) -> int:
        return self._subrtn.sample_count

    def compute_params(self, sim_instances: to.Tensor, t: int):
        """
        Compute the parameters.

        :param sim_instances: Physics configurations rollout
        :param t: time step to chose
        :return: parameters at the time
        """
        nominal = self.svpg_wrapper.nominal_dict()
        keys = nominal.keys()
        assert len(keys) == sim_instances[t][0].shape[0]

        params = []
        for sim_instance in sim_instances[t]:
            d = {k: (sim_instance[i] + 0.5) * (nominal[k]) for i, k in enumerate(keys)}
            params.append(d)

        return params

    def step(self, snapshot_mode: str, meta_info: dict = None):
        rand_trajs = []
        ref_trajs = []
        ros = []
        for i, p in enumerate(self.svpg.iter_particles):
            done = False
            svpg_env = self.svpg_wrapper
            state = svpg_env.reset(i)
            states = []
            actions = []
            rewards = []
            infos = []
            rand_trajs_now = []
            exploration_logbook = []
            with to.no_grad():
                while not done:
                    action = p.expl_strat(to.as_tensor(state, dtype=to.get_default_dtype())).detach().cpu().numpy()
                    state, reward, done, info = svpg_env.step(action, i)
                    state_dict = svpg_env.array_to_dict((state + 0.5) * svpg_env.nominal())
                    print(state_dict, " => ", reward)

                    # Log visited states as dict
                    if self.log_exploration:
                        exploration_logbook.append(state_dict)

                    # Store rollout results
                    states.append(state)
                    rewards.append(reward)
                    actions.append(action)
                    infos.append(info)

                    # Extract trajectories from info
                    rand_trajs_now.extend(info["rand"])
                    rand_trajs += info["rand"]
                    ref_trajs += info["ref"]
                ros.append(StepSequence(observations=states, actions=actions, rewards=rewards))
            self.logger.add_value(f"SVPG_agent_{i}_mean_reward", np.mean(rewards))
            ros[i].torch(data_type=to.DoubleTensor)
            # rand_trajs_now = StepSequence.concat(rand_trajs_now)
            for rt in rand_trajs_now:
                self.convert_and_detach(rt)
            self._subrtn.update(rand_trajs_now)

        # Logging
        rets = [ro.undiscounted_return() for ro in rand_trajs]
        ret_avg = np.mean(rets)
        ret_med = np.median(rets)
        ret_std = np.std(rets)
        self.logger.add_value("avg rollout len", np.mean([ro.length for ro in rand_trajs]))
        self.logger.add_value("avg return", ret_avg)
        self.logger.add_value("median return", ret_med)
        self.logger.add_value("std return", ret_std)

        # Flatten and combine all randomized and reference trajectories for discriminator
        flattened_randomized = StepSequence.concat(rand_trajs)
        flattened_randomized.torch(data_type=to.double)
        flattened_reference = StepSequence.concat(ref_trajs)
        flattened_reference.torch(data_type=to.double)
        self.reward_generator.train(flattened_reference, flattened_randomized, self.num_discriminator_epoch)
        pyrado.save(
            self.reward_generator.discriminator, "discriminator.pt", self.save_dir, prefix="adr", use_state_dict=True
        )

        if self.curr_time_step > self.warm_up_time:
            # Update the particles
            # List of lists to comply with interface
            self.svpg.update(list(map(lambda x: [x], ros)))
        self.convert_and_detach(flattened_randomized)
        # np.save(f'{self.save_dir}actions{self.curr_iter}', flattened_randomized.actions)
        self.make_snapshot(snapshot_mode, float(ret_avg), meta_info)
        self._subrtn.make_snapshot(snapshot_mode="best", curr_avg_ret=float(ret_avg))
        self.curr_time_step += 1

    def convert_and_detach(self, arg0):
        arg0.torch(data_type=to.float)
        arg0.observations = arg0.observations.float().detach()
        arg0.actions = arg0.actions.float().detach()

    def save_snapshot(self, meta_info: dict = None):
        super().save_snapshot(meta_info)

        if meta_info is not None:
            raise pyrado.ValueErr(msg=f"{self.name} is not supposed be run as a subrtn!")

        # This algorithm instance is not a subrtn of another algorithm
        pyrado.save(self.env, "env.pkl", self.save_dir)
        self._subrtn.save_snapshot(meta_info=meta_info)
        # self.svpg.save_snapshot(meta_info)


class SVPGAdapter(EnvWrapper, Serializable):
    """Wrapper to encapsulate the domain parameter search as an RL task."""

    def __init__(
        self,
        wrapped_env: Env,
        parameters: Sequence[DomainParam],
        inner_policy: Policy,
        discriminator,
        num_particles: int,
        step_length: float = 0.01,
        horizon: int = 50,
        num_rollouts_per_config: int = 8,
        num_workers: int = 4,
        max_steps: int = 8,
    ):
        """
        Constructor

        :param wrapped_env: the environment to wrap
        :param parameters: which physics parameters should be randomized
        :param inner_policy: the policy to train the subrtn on
        :param discriminator: the discriminator to distinguish reference environments from randomized ones
        :param step_length: the step size
        :param horizon: an svpg horizon
        :param num_rollouts_per_config: number of trajectories to sample per physics configuration
        :param num_workers: number of environments for parallel sampling
        """
        Serializable._init(self, locals())

        EnvWrapper.__init__(self, wrapped_env)

        self.parameters: Sequence[DomainParam] = parameters
        try:
            self.pool = SamplerPool(num_workers)
        except AssertionError:
            Warning("THIS IS NOT MEANT TO BE PARALLEL SAMPLED")
        self.inner_policy = inner_policy
        self.num_particles = num_particles
        self.inner_parameter_state: np.ndarray = np.zeros((self.num_particles, len(self.parameters)))
        self.count = np.zeros(self.num_particles)
        self.num_trajs = num_rollouts_per_config
        self.svpg_max_step_length = step_length
        self.discriminator = discriminator
        self.max_steps = max_steps
        self._adapter_obs_space = BoxSpace(-np.ones(len(parameters)), np.ones(len(parameters)))
        self._adapter_act_space = BoxSpace(-np.ones(len(parameters)), np.ones(len(parameters)))
        self.horizon = horizon
        self.horizon_count = 0

        self.reset()

    @property
    def obs_space(self) -> Space:
        return self._adapter_obs_space

    @property
    def act_space(self) -> Space:
        return self._adapter_act_space

    def reset(self, i=None, init_state: np.ndarray = None, domain_param: dict = None) -> np.ndarray:
        if i is not None:
            assert domain_param is None
            self.count[i] = 0
            if init_state is None:
                self.inner_parameter_state[i] = np.random.random_sample(len(self.parameters))
            else:
                self.inner_parameter_state[i] = init_state
            return self.inner_parameter_state[i]

        assert domain_param is None
        self.count = np.zeros(self.num_particles)
        if init_state is None:
            self.inner_parameter_state = np.random.random_sample((self.num_particles, len(self.parameters)))
        else:
            self.inner_parameter_state = init_state
        return self.inner_parameter_state

    def step(self, act: np.ndarray, i: int) -> tuple:
        if i is not None:
            # Clip the action according to the maximum step length
            action = np.clip(act, -1, 1) * self.svpg_max_step_length

            # Perform step by moving into direction of action
            self.inner_parameter_state[i] = np.clip(self.inner_parameter_state[i] + action, 0, 1)
            param_norm = self.inner_parameter_state[i] + 0.5
            random_parameters = [self.array_to_dict(param_norm * self.nominal())] * self.num_trajs
            nominal_parameters = [self.nominal_dict()] * self.num_trajs

            # Sample trajectories from random and reference environments
            rand = eval_domain_params(self.pool, self.wrapped_env, self.inner_policy, random_parameters)
            ref = eval_domain_params(self.pool, self.wrapped_env, self.inner_policy, nominal_parameters)

            # Calculate the rewards for each trajectory
            rewards = [self.discriminator.get_reward(traj) for traj in rand]
            reward = np.mean(rewards)
            info = dict(rand=rand, ref=ref)

            # Handle step count management
            done = self.count[i] >= self.max_steps - 1
            self.count[i] += 1
            self.horizon_count += 1
            if self.count[i] % self.horizon == 0:
                self.inner_parameter_state[i] = np.random.random_sample(len(self.parameters))

            return self.inner_parameter_state[i], reward, done, info

        raise NotImplementedError("Not parallelizable")

    def eval_states(self, states: Sequence[np.ndarray]):
        """
        Evaluate the states.

        :param states: the states to evaluate
        :return: respective rewards and according trajectories
        """
        flatten = lambda l: [item for sublist in l for item in sublist]
        sstates = flatten([[self.array_to_dict((state + 0.5) * self.nominal())] * self.num_trajs for state in states])
        rand = eval_domain_params(self.pool, self.wrapped_env, self.inner_policy, sstates)
        ref = eval_domain_params(
            self.pool, self.wrapped_env, self.inner_policy, [self.nominal_dict()] * (self.num_trajs * len(states))
        )
        rewards = [self.discriminator.get_reward(traj) for traj in rand]
        rewards = [np.mean(rewards[i * self.num_trajs : (i + 1) * self.num_trajs]) for i in range(len(states))]
        return rewards, rand, ref

    def params(self):
        return [param.name for param in self.parameters]

    def nominal(self):
        return [inner_env(self.wrapped_env).get_nominal_domain_param()[k] for k in self.params()]

    def nominal_dict(self):
        return {k: inner_env(self.wrapped_env).get_nominal_domain_param()[k] for k in self.params()}

    def array_to_dict(self, arr):
        return {k: a for k, a in zip(self.params(), arr)}


class RewardGenerator:
    """Class for generating the discriminator rewards in ADR. Generates a reward using a trained discriminator network."""

    def __init__(
        self,
        env_spec: EnvSpec,
        batch_size: int,
        reward_multiplier: float,
        lr: float = 3e-3,
        hidden_size=256,
        logger: StepLogger = None,
        device: str = "cuda" if to.cuda.is_available() else "cpu",
    ):

        """
        Constructor

        :param env_spec: environment specification
        :param batch_size: batch size for each update step
        :param reward_multiplier: factor for the predicted probability
        :param lr: learning rate
        :param logger: logger for every step of the algorithm, if `None` the default logger will be created
        """
        self.device = device
        self.batch_size = batch_size
        self.reward_multiplier = reward_multiplier
        self.lr = lr
        spec = EnvSpec(
            obs_space=BoxSpace.cat([env_spec.obs_space, env_spec.act_space]),
            act_space=BoxSpace(bound_lo=[0], bound_up=[1]),
        )
        self.discriminator = LSTMPolicy(
            spec=spec, hidden_size=hidden_size, num_recurrent_layers=1, output_nonlin=to.sigmoid
        )
        self.loss_fcn = nn.BCELoss()
        self.optimizer = to.optim.Adam(self.discriminator.parameters(), lr=lr, eps=1e-5)
        self.logger = logger

    def get_reward(self, traj: StepSequence) -> to.Tensor:
        """Compute the reward of a trajectory.
        Trajectories considered as not fixed yield a high reward.

        :param traj: trajectory to evaluate
        :return: a score
        :rtype: to.Tensor
        """
        traj = preprocess_rollout(traj)
        with to.no_grad():
            reward = self.discriminator.forward(traj)[0]
            return to.log(reward.mean()) * self.reward_multiplier

    def train(
        self, reference_trajectory: StepSequence, randomized_trajectory: StepSequence, num_epoch: int
    ) -> to.Tensor:

        reference_batch_generator = reference_trajectory.iterate_rollouts()
        random_batch_generator = randomized_trajectory.iterate_rollouts()

        loss = None
        for _ in tqdm(range(num_epoch), "Discriminator Epoch", num_epoch):
            for reference_batch, random_batch in zip(reference_batch_generator, random_batch_generator):
                reference_batch = preprocess_rollout(reference_batch).float()
                random_batch = preprocess_rollout(random_batch).float()
                random_results = self.discriminator(random_batch)[0]
                reference_results = self.discriminator(reference_batch)[0]
                self.optimizer.zero_grad()
                loss = self.loss_fcn(random_results, to.ones(random_results.shape[0], 1)) + self.loss_fcn(
                    reference_results, to.zeros(reference_results.shape[0], 1)
                )
                loss.backward()
                self.optimizer.step()
                # Logging
        if self.logger is not None:
            self.logger.add_value("discriminator_loss", loss)
        return loss


def preprocess_rollout(rollout: StepSequence) -> Tensor:
    """
    Extract observations and actions from a `StepSequence` and packs them into a PyTorch tensor.

    :param rollout: a `StepSequence` instance containing a trajectory
    :return: a PyTorch tensor` containing the trajectory
    """
    if not isinstance(rollout, StepSequence):
        raise pyrado.TypeErr(given=rollout, expected_type=StepSequence)

    # Convert data type
    rollout.torch(to.get_default_dtype())

    # Extract the data
    state = rollout.get_data_values("observations")[:-1]
    next_state = rollout.get_data_values("observations")[1:]
    action = rollout.get_data_values("actions").narrow(0, 0, next_state.shape[0])

    return to.cat((state, action), 1)
