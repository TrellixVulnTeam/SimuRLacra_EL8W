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

import time
from typing import Optional

import numpy as np
import robcom_python as robcom
from scipy.spatial.transform import Rotation

import pyrado
from pyrado.environments.barrett_wam import (
    ACT_SPACE_BIC_4DOF,
    ACT_SPACE_BIC_7DOF,
    CUP_POS_INIT_SIM_4DOF,
    CUP_POS_INIT_SIM_7DOF,
    WAM_Q_LIMITS_LO_7DOF,
    WAM_Q_LIMITS_UP_7DOF,
    WAM_QD_LIMITS_LO_7DOF,
    WAM_QD_LIMITS_UP_7DOF,
)
from pyrado.environments.barrett_wam.natnet_client import NatNetClient
from pyrado.environments.barrett_wam.trackers import RigidBodyTracker
from pyrado.environments.barrett_wam.wam_base import WAMReal
from pyrado.spaces import BoxSpace
from pyrado.spaces.base import Space
from pyrado.tasks.base import Task
from pyrado.tasks.final_reward import FinalRewMode, FinalRewTask
from pyrado.tasks.goalless import GoallessTask
from pyrado.tasks.reward_functions import ZeroPerStepRewFcn
from pyrado.utils.input_output import completion_context, print_cbt, print_cbt_once


class WAMBallInCupRealEpisodic(WAMReal):
    """
    Class for the real Barrett WAM solving the ball-in-the-cup task using an episodic policy.

    Uses robcom 2.0 and specifically robcom's ClosedLoopDirectControl` process to execute a trajectory
    given by desired joint positions. The control process is only executed on the real system after `max_steps` has been
    reached to avoid possible latency, but at the same time mimic the usual step-based environment behavior.
    """

    name: str = "wam-bic"

    def __init__(
        self,
        num_dof: int,
        max_steps: int,
        dt: float = 1 / 500.0,
        ip: Optional[str] = "192.168.2.2",
    ):
        """
        Constructor

        :param num_dof: number of degrees of freedom (4 or 7), depending on which Barrett WAM setup being used
        :param max_steps: maximum number of time steps
        :param dt: sampling time interval
        :param ip: IP address of the PC controlling the Barrett WAM, pass `None` to skip connecting
        """
        # Call WAMReal's constructor
        super().__init__(dt=dt, max_steps=max_steps, num_dof=num_dof, ip=ip)

        # Use a subset of joints for the ball-in-the-cup task
        self._idcs_act = [1, 3] if self._num_dof == 4 else [1, 3, 5]

        self._curr_step_rr = None

    @property
    def state_space(self) -> Space:
        # Normalized time
        return BoxSpace(np.array([0.0]), np.array([1.0]), labels=["t"])

    @property
    def obs_space(self) -> Space:
        # Observation space (normalized time)
        return self.state_space

    @property
    def act_space(self) -> Space:
        # Running a PD controller on joint positions and velocities
        return ACT_SPACE_BIC_7DOF if self._num_dof == 7 else ACT_SPACE_BIC_4DOF

    def _create_task(self, task_args: dict) -> Task:
        # The wrapped task acts as a dummy and carries the FinalRewTask
        return FinalRewTask(
            GoallessTask(self.spec, ZeroPerStepRewFcn()),
            mode=FinalRewMode(user_input=True),
        )

    def reset(self, init_state: np.ndarray = None, domain_param: dict = None) -> np.ndarray:
        # Call WAMReal's reset
        super().reset(init_state, domain_param)

        # Reset current step of the real robot
        self._curr_step_rr = 0

        # Reset state
        self.state = np.array([self._curr_step / self.max_steps])

        # Reset trajectory params
        self.qpos_des = np.tile(self._qpos_des_init, (self.max_steps, 1))
        self.qvel_des = np.zeros_like(self.qpos_des)

        # Create robcom direct-control process
        self._dc = self._client.create(robcom.ClosedLoopDirectControl, self._robot_group_name, "")

        input("Hit enter to continue.")
        return self.observe(self.state)

    def step(self, act: np.ndarray) -> tuple:
        if self._curr_step == 0:
            print_cbt("Pre-sampling policy...", "w")

        info = dict(act_raw=act.copy())

        # Current reward depending on the (measurable) state and the current (unlimited) action
        remaining_steps = self._max_steps - (self._curr_step + 1) if self._max_steps is not pyrado.inf else 0
        self._curr_rew = self._task.step_rew(self.state, act, remaining_steps)  # always 0 for wam-bic-real

        # Limit the action
        act = self.limit_act(act)

        # The policy operates on specific indices self._idcs_act, i.e. joint 1 and 3 (and 5)
        self.qpos_des[self._curr_step, self._idcs_act] += act[: len(self._idcs_act)]
        self.qvel_des[self._curr_step, self._idcs_act] += act[len(self._idcs_act) :]

        # Update current step and state
        self._curr_step += 1
        self.state = np.array([self._curr_step / self.max_steps])

        # A GoallessTask only signals done when has_failed() is true, i.e. the the state is out of bounds
        done = self._task.is_done(self.state)  # always false for wam-bic-real

        # Only start execution of process when all desired poses have been sampled from the policy
        if self._curr_step >= self._max_steps:
            done = True  # exceeded max time steps
            with completion_context("Executing trajectory on Barret WAM", color="c", bright=True):
                self._dc.start(False, round(500 * self._dt), self._callback, ["POS", "VEL"], [], [])
                t_start = time.time()
                self._dc.wait_for_completion()
                t_stop = time.time()
            print_cbt(f"Execution took {t_stop - t_start:1.5f} s.", "g")

        # Add final reward if done
        if done:
            # Ask the user to enter the final reward
            self._curr_rew += self._task.final_rew(self.state, remaining_steps)

            # Stop robcom data streaming
            self._client.set(robcom.Streaming, False)

        return self.observe(self.state), self._curr_rew, done, info

    def _callback(self, jg, eg, data_provider):
        """
        This function is called from robcom's ClosedLoopDirectControl process as callback and should never be called manually

        :param jg: joint group
        :param eg: end-effector group
        :param data_provider: additional data stream
        """
        # Check if max_steps is reached
        if self._curr_step_rr >= self.max_steps:
            return True

        # Get current joint position and velocity for storing
        self.qpos_real[self._curr_step_rr] = np.array(jg.get(robcom.JointState.POS))
        self.qvel_real[self._curr_step_rr] = np.array(jg.get(robcom.JointState.VEL))

        # Set desired joint position and velocity
        dpos = self.qpos_des[self._curr_step_rr].tolist()
        dvel = self.qvel_des[self._curr_step_rr].tolist()
        jg.set(robcom.JointDesState.POS, dpos)
        jg.set(robcom.JointDesState.VEL, dvel)

        # Update current step at real robot
        self._curr_step_rr += 1

        return False


class WAMBallInCupRealStepBased(WAMReal):
    """
    Class for the real Barrett WAM solving the ball-in-the-cup task using a step-based policy.

    Uses robcom 2.0 and specifically robcom's `CosedLoopDirectControl` process to execute a trajectory
    given by desired joint positions. The control process is running in a separate thread and is executed on the real
    system simultaneous to the step function calls. Includes the option to observe ball and cup using OptiTrack.
    """

    name: str = "wam-bic"

    def __init__(
        self,
        observe_ball: bool,
        observe_cup: bool,
        num_dof: int,
        max_steps: int,
        dt: float = 1 / 500.0,
        ip: Optional[str] = "192.168.2.2",
    ):
        """
        Constructor

        :param observe_ball: if `True`, include the 2-dim (x-z plane) cartesian ball position into the observation
        :param observe_cup: if `True`, include the 2-dim (x-z plane) cartesian cup position into the observation
        :param num_dof: number of degrees of freedom (4 or 7), depending on which Barrett WAM setup being used
        :param max_steps: maximum number of time steps
        :param dt: sampling time interval
        :param ip: IP address of the PC controlling the Barrett WAM, pass `None` to skip connecting
        """
        self.observe_ball = observe_ball
        self.observe_cup = observe_cup

        # Call WAMReal's constructor
        super().__init__(dt=dt, max_steps=max_steps, num_dof=num_dof, ip=ip)

        self._ram = None  # robot access manager is set in reset()
        self._cnt_too_slow = None

        # Use a subset of joints for the ball-in-the-cup task
        self._idcs_act = [1, 3] if self._num_dof == 4 else [1, 3, 5]

        # Create OptiTrack client
        self.natnet_client = NatNetClient(ver=(3, 0, 0, 0), quiet=True)
        self.rigid_body_tracker = RigidBodyTracker(
            ["Cup", "Ball"],
            rotation=Rotation.from_euler("yxz", [-90.0, 90.0, 0.0], degrees=True),
        )
        self.natnet_client.rigidBodyListener = self.rigid_body_tracker

    @property
    def state_space(self) -> Space:
        # State space (joint positions and velocities)
        state_lo = np.concatenate([WAM_Q_LIMITS_LO_7DOF[: self._num_dof], WAM_QD_LIMITS_LO_7DOF[: self._num_dof]])
        state_up = np.concatenate([WAM_Q_LIMITS_UP_7DOF[: self._num_dof], WAM_QD_LIMITS_UP_7DOF[: self._num_dof]])

        # Ball and cup (x,y,z)-space
        if self.observe_ball:
            state_lo = np.r_[state_lo, np.full((3,), -3.0)]
            state_up = np.r_[state_up, np.full((3,), 3.0)]
        if self.observe_cup:
            state_lo = np.r_[state_lo, np.full((3,), -3.0)]
            state_up = np.r_[state_up, np.full((3,), 3.0)]

        return BoxSpace(state_lo, state_up)

    @property
    def obs_space(self) -> Space:
        # Observation space (normalized time and optionally cup and ball position)
        obs_lo, obs_up, labels = [0.0], [1.0], ["t"]

        if self.observe_ball:
            obs_lo.extend([-3.0, -3.0])
            obs_up.extend([3.0, 3.0])
            labels.extend(["ball_x", "ball_z"])
        if self.observe_cup:
            obs_lo.extend([-3.0, -3.0])
            obs_up.extend([3.0, 3.0])
            labels.extend(["cup_x", "cup_z"])

        return BoxSpace(obs_lo, obs_up, labels=labels)

    @property
    def act_space(self) -> Space:
        # Running a PD controller on joint positions and velocities
        return ACT_SPACE_BIC_7DOF if self._num_dof == 7 else ACT_SPACE_BIC_4DOF

    def _create_task(self, task_args: dict) -> Task:
        # The wrapped task acts as a dummy and carries the FinalRewTask
        return FinalRewTask(
            GoallessTask(self.spec, ZeroPerStepRewFcn()),
            mode=FinalRewMode(user_input=True),
        )

    def reset(self, init_state: np.ndarray = None, domain_param: dict = None) -> np.ndarray:
        # Call WAMReal's reset
        super().reset(init_state, domain_param)

        # Get the robot access manager, to control that synchronized data is received
        self._ram = robcom.RobotAccessManager()

        # Reset desired positions and velocities
        self.qpos_des = self._qpos_des_init.copy()
        self.qvel_des = np.zeros_like(self._qpos_des_init)

        # Create robcom direct-control process
        self._dc = self._client.create(robcom.DirectControl, self._robot_group_name, "")

        # Start NatNet client only once
        if self.natnet_client.dataSocket is None or self.natnet_client.commandSocket is None:
            self.natnet_client.run()

            # If the rigid body tracker is not ready yet, get_current_estimate() will throw an error
            with completion_context("Initializing rigid body tracker", color="c"):
                while not self.rigid_body_tracker.initialized():
                    time.sleep(0.05)

        # Determine offset for the rigid body tracker (from OptiTrack to MuJoCo)
        cup_pos_init_sim = CUP_POS_INIT_SIM_4DOF if self._num_dof == 4 else CUP_POS_INIT_SIM_7DOF
        self.rigid_body_tracker.reset_offset()
        offset = self.rigid_body_tracker.get_current_estimate(["Cup"])[0] - cup_pos_init_sim
        self.rigid_body_tracker.offset = offset

        # Get current joint state
        self.state = np.concatenate(self._get_joint_state())

        # Set the time for the busy waiting sleep call in step()
        self._t = time.time()
        self._cnt_too_slow = 0

        input("Hit enter to continue.")
        return self.observe(self.state)

    def _get_joint_state(self):
        """
        Use robcom's streaming to get the current joint state

        :return: joint positions, joint velocities
        """
        self._ram.lock()
        qpos = self._jg.get(robcom.JointState.POS)
        qvel = self._jg.get(robcom.JointState.VEL)
        self._ram.unlock()

        return qpos, qvel

    def step(self, act: np.ndarray) -> tuple:
        # Start robcom direct-control process
        if self._curr_step == 0:
            print_cbt("Executing trajectory on Barret WAM", color="c", bright=True)
            self._dc.start()

        info = dict(act_raw=act.copy())

        # Current reward depending on the (measurable) state and the current (unlimited) action
        remaining_steps = self._max_steps - (self._curr_step + 1) if self._max_steps is not pyrado.inf else 0
        self._curr_rew = self._task.step_rew(self.state, act, remaining_steps)  # always 0 for wam-bic-real

        # Limit the action
        act = self.limit_act(act)

        # The policy operates on specific indices self._idcs_act, i.e. joint 1 and 3 (and 5)
        self.qpos_des[self._idcs_act] = self._qpos_des_init[self._idcs_act] + act[: len(self._idcs_act)]
        self.qvel_des[self._idcs_act] = act[len(self._idcs_act) :]

        # Send desired positions and velocities to robcom
        self._dc.groups.set(robcom.JointDesState.POS, self.qpos_des)
        self._dc.groups.set(robcom.JointDesState.VEL, self.qvel_des)
        self._dc.send_updates()

        # Sleep to keep the frequency
        to_sleep = self._dt - (time.time() - self._t)
        if to_sleep > 0.0:
            time.sleep(to_sleep)
        else:
            self._cnt_too_slow += 1
        self._t = time.time()

        # Get current joint angles and angular velocities
        qpos, qvel = self._get_joint_state()
        self.state = np.concatenate([qpos, qvel])
        self.qpos_real[self._curr_step] = qpos
        self.qvel_real[self._curr_step] = qvel

        # Get the OptiTrack
        if self.observe_ball:
            ball_pos, _ = self.rigid_body_tracker.get_current_estimate(["Ball"])
            self.state = np.r_[self.state, ball_pos]
        if self.observe_cup:
            cup_pos, _ = self.rigid_body_tracker.get_current_estimate(["Cup"])
            self.state = np.r_[self.state, cup_pos]

        # Update current step and state
        self._curr_step += 1

        # A GoallessTask only signals done when has_failed() is true, i.e. the the state is out of bounds
        done = self._task.is_done(self.state)  # always false for wam-bic-real

        # Check if exceeded max time steps
        if self._curr_step >= self._max_steps:
            done = True

        # Add final reward if done
        if done:
            # Ask the user to enter the final reward
            self._curr_rew += self._task.final_rew(self.state, remaining_steps)

            # Stop robcom direct-control process
            self._dc.stop()

            # Stop robcom data streaming
            self._client.set(robcom.Streaming, False)

            print_cbt(
                f"The step call was too slow for the control frequency {self._cnt_too_slow} out of "
                f"{self._curr_step} times.",
                color="y",
            )

        return self.observe(self.state), self._curr_rew, done, info

    def observe(self, state: np.ndarray) -> np.ndarray:
        # Observe the normalized time
        obs = np.array([self._curr_step / self.max_steps])

        # Extract the (x, z) cartesian position of cup and ball (the robot operates in the x-z plane).
        # Note: the cup_goal is the mujoco site object marking the goal position for the ball. It is not identical
        # to the coordinate system origin of the rigid body object 'cup'
        if self.observe_ball:
            obs = np.r_[obs, state[-3], state[-1]]
        if self.observe_cup:
            obs = np.r_[obs, state[-6], state[-4]]

        return obs

    def close(self):
        self.natnet_client.stop()
