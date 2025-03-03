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

"""
Train an agent to solve the Quanser Qube swing-up task using Self-Paced Domain Radnomization using Proximal Policy Optimization
as a subroutine.
"""
import numpy as np
import torch as to
from torch.optim import lr_scheduler

import pyrado
from pyrado.algorithms.meta.spdr import SPDR
from pyrado.algorithms.step_based.gae import GAE
from pyrado.algorithms.step_based.ppo import PPO
from pyrado.domain_randomization.domain_parameter import SelfPacedDomainParam
from pyrado.domain_randomization.domain_randomizer import DomainRandomizer
from pyrado.environment_wrappers.action_normalization import ActNormWrapper
from pyrado.environment_wrappers.domain_randomization import DomainRandWrapperLive
from pyrado.environments.pysim.quanser_qube import QQubeSwingUpSim
from pyrado.logger.experiment import save_dicts_to_yaml, setup_experiment
from pyrado.policies.feed_back.fnn import FNNPolicy
from pyrado.spaces import ValueFunctionSpace
from pyrado.utils.argparser import get_argparser
from pyrado.utils.data_types import EnvSpec


if __name__ == "__main__":
    # Parse command line arguments
    parser = get_argparser()
    parser.add_argument("--frequency", default=250, type=int)
    parser.set_defaults(max_steps=600)
    parser.add_argument("--ppo_iterations", default=150, type=int)
    parser.add_argument("--spdr_iterations", default=50, type=int)
    parser.add_argument("--cov_only", action="store_true")
    args = parser.parse_args()

    # Experiment (set seed before creating the modules)
    ex_dir = setup_experiment(
        QQubeSwingUpSim.name,
        f"{PPO.name}_{FNNPolicy.name}",
        f"{args.frequency}Hz_covonly_{args.cov_only}_seed_{args.seed}",
    )

    # Set seed if desired
    pyrado.set_seed(args.seed, verbose=True)

    # Environment
    env_hparams = dict(dt=1 / float(args.frequency), max_steps=args.max_steps)
    env = QQubeSwingUpSim(**env_hparams)
    env = ActNormWrapper(env)

    # Policy
    policy_hparam = dict(hidden_sizes=[64, 64], hidden_nonlin=to.tanh)  # FNN
    # policy_hparam = dict(hidden_size=32, num_recurrent_layers=1)  # LSTM & GRU
    policy = FNNPolicy(spec=env.spec, **policy_hparam)
    # policy = GRUPolicy(spec=env.spec, **policy_hparam)

    # Critic
    vfcn_hparam = dict(hidden_sizes=[32, 32], hidden_nonlin=to.relu)  # FNN
    # vfcn_hparam = dict(hidden_size=32, num_recurrent_layers=1)  # LSTM & GRU
    vfcn = FNNPolicy(spec=EnvSpec(env.obs_space, ValueFunctionSpace), **vfcn_hparam)
    # vfcn = GRUPolicy(spec=EnvSpec(env.obs_space, ValueFunctionSpace), **vfcn_hparam)
    critic_hparam = dict(
        gamma=0.9844224855479998,
        lamda=0.9700148505302241,
        num_epoch=5,
        batch_size=500,
        standardize_adv=False,
        lr=7.058326426522811e-4,
        max_grad_norm=6.0,
        lr_scheduler=lr_scheduler.ExponentialLR,
        lr_scheduler_hparam=dict(gamma=0.999),
    )
    critic = GAE(vfcn, **critic_hparam)

    # Subroutine
    algo_hparam = dict(
        max_iter=args.ppo_iterations,
        eps_clip=0.12648736789309026,
        min_steps=30 * env.max_steps,
        num_epoch=7,
        batch_size=500,
        std_init=0.7573286998997557,
        lr=6.999956625305722e-04,
        max_grad_norm=1.0,
        num_workers=8,
        lr_scheduler=lr_scheduler.ExponentialLR,
        lr_scheduler_hparam=dict(gamma=0.999),
    )
    env_spdr_param = dict(
        name=["gravity_const", "motor_resistance"],
        target_mean=to.tensor([9.81, 8.4]),
        target_cov_flat=to.tensor([1.0, 1.0]),
        init_mean=to.tensor([9.81, 8.4]),
        init_cov_flat=to.tensor([0.01, 0.01]),
        clip_lo=-pyrado.inf,
        clip_up=+pyrado.inf,
    )
    env = DomainRandWrapperLive(env, randomizer=DomainRandomizer(SelfPacedDomainParam(**env_spdr_param)))

    spdr_hparam = dict(
        kl_constraints_ub=10,
        performance_lower_bound=250,
        kl_threshold=-np.inf,
        max_iter=args.spdr_iterations,
        optimize_mean=not args.cov_only,
        max_subrtn_retries=1,
    )
    algo = SPDR(env, PPO(ex_dir, env, policy, critic, **algo_hparam), **spdr_hparam)

    # Save the hyper-parameters
    save_dicts_to_yaml(
        dict(env=env_hparams, seed=args.seed),
        dict(policy=policy_hparam),
        dict(critic=critic_hparam, vfcn=vfcn_hparam),
        dict(subrtn=algo_hparam, subrtn_name=PPO.name),
        dict(algo=spdr_hparam, algo_name=algo.name, env_spdr_param=env_spdr_param),
        save_dir=ex_dir,
    )

    # Jeeeha
    algo.train(snapshot_mode="latest", seed=args.seed)
