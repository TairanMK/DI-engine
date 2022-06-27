from typing import TYPE_CHECKING, Optional, Callable, List, Tuple, Any
from easydict import EasyDict
from functools import reduce
import treetensor.torch as ttorch
from ding.envs import BaseEnvManager
from ding.policy import Policy
import torch
from ding.utils import dicts_to_lists
from ding.torch_utils import to_tensor, to_ndarray
from ding.framework import task

# if TYPE_CHECKING:
from ding.framework import OnlineRLContext, BattleContext
from collections import deque
from ding.framework.middleware.functional.actor_data import ActorEnvTrajectories
from dizoo.distar.envs.fake_data import rl_step_data

from ditk import logging


class TransitionList:

    def __init__(self, env_num: int) -> None:
        self.env_num = env_num
        self._transitions = [[] for _ in range(env_num)]
        self._done_idx = [[] for _ in range(env_num)]

    def append(self, env_id: int, transition: Any) -> None:
        self._transitions[env_id].append(transition)
        if transition.done:
            self._done_idx[env_id].append(len(self._transitions[env_id]))

    def to_trajectories(self) -> Tuple[List[Any], List[int]]:
        trajectories = sum(self._transitions, [])
        lengths = [len(t) for t in self._transitions]
        trajectory_end_idx = [reduce(lambda x, y: x + y, lengths[:i + 1]) for i in range(len(lengths))]
        trajectory_end_idx = [t - 1 for t in trajectory_end_idx]
        return trajectories, trajectory_end_idx

    def to_episodes(self) -> List[List[Any]]:
        episodes = []
        for env_id in range(self.env_num):
            last_idx = 0
            for done_idx in self._done_idx[env_id]:
                episodes.append(self._transitions[env_id][last_idx:done_idx])
                last_idx = done_idx
        return episodes

    def clear(self):
        for item in self._transitions:
            item.clear()
        for item in self._done_idx:
            item.clear()


class BattleTransitionList:

    def __init__(self, env_num: int, unroll_len: int) -> None:
        # for each env, we have a deque to buffer episodes,
        # and a deque to tell each episode is finished or not
        self.env_num = env_num
        self._transitions = [deque() for _ in range(env_num)]
        self._done_episode = [deque() for _ in range(env_num)]
        self._unroll_len = unroll_len
        # TODO(zms): last transition + 1

    def get_env_trajectories(self, env_id: int, only_finished: bool = False) -> List[List]:
        trajectories = []
        if len(self._transitions[env_id]) == 0:
            # if we have no episode for this env, we return an empty list
            return trajectories
        while len(self._transitions[env_id]) > 0:
            # Every time we check if oldest episode is done,
            # if is done, we cut the episode to trajectories
            # and finally drop this episode
            if self._done_episode[env_id][0] is False:
                break
            oldest_episode = self._transitions[env_id].popleft()
            self._done_episode[env_id].popleft()
            trajectories += self._cut_trajectory_from_episode(oldest_episode)
            oldest_episode.clear()

        if not only_finished and len(self._transitions[env_id]) == 1 and self._done_episode[env_id][0] is False:
            # If last episode is not done, we only cut the trajectories till the Trajectory(t-1) (not including)
            # This is because we need Trajectory(t-1) to fill up Trajectory(t) if in Trajectory(t) this episode is done
            tail_idx = max(
                0, ((len(self._transitions[env_id][0]) - self._unroll_len) // self._unroll_len) * self._unroll_len
            )
            trajectories += self._cut_trajectory_from_episode(self._transitions[env_id][0][:tail_idx])
            self._transitions[env_id][0] = self._transitions[env_id][0][tail_idx:]

        return trajectories

    def to_trajectories(self, only_finished: bool = False) -> List[ActorEnvTrajectories]:
        all_env_data = []
        for env_id in range(self.env_num):
            trajectories = self.get_env_trajectories(env_id, only_finished=only_finished)
            if len(trajectories) > 0:
                all_env_data.append(ActorEnvTrajectories(env_id=env_id, trajectories=trajectories))
        return all_env_data

    def _cut_trajectory_from_episode(self, episode: list) -> List[List]:
        # first we cut complete trajectories (list of transitions whose length equal to unroll_len)
        # then we gather the transitions in the tail of episode, and fill up the trajectory with the tail transitions in Trajectory(t-1)
        # If we don't have Trajectory(t-1), i.e. the length of the whole episode is smaller than unroll_len, we fill up the trajectory
        # with the first element of episode.
        return_episode = []
        i = 0
        num_complele_trajectory, num_tail_transitions = divmod(len(episode), self._unroll_len)
        for i in range(num_complele_trajectory):
            trajectory = episode[i * self._unroll_len:(i + 1) * self._unroll_len]
            # TODO(zms): 测试专用，之后去掉
            trajectory.append(rl_step_data(last=True))
            return_episode.append(trajectory)

        if num_tail_transitions > 0:
            trajectory = episode[-self._unroll_len:]
            if len(trajectory) < self._unroll_len:
                initial_elements = []
                for _ in range(self._unroll_len - len(trajectory)):
                    initial_elements.append(trajectory[0])
                trajectory = initial_elements + trajectory
            # TODO(zms): 测试专用，之后去掉
            trajectory.append(rl_step_data(last=True))
            return_episode.append(trajectory)

        return return_episode  # list of trajectories

    def clear_newest_episode(self, env_id: int) -> None:
        # Use it when env.step raise some error
        newest_episode = self._transitions[env_id].pop()
        len_newest_episode = len(newest_episode)
        newest_episode.clear()
        self._done_episode[env_id].pop()
        return len_newest_episode

    def append(self, env_id: int, transition: Any) -> bool:
        # If previous episode is done, we create a new episode
        if len(self._done_episode[env_id]) == 0 or self._done_episode[env_id][-1] is True:
            self._transitions[env_id].append([])
            self._done_episode[env_id].append(False)
        self._transitions[env_id][-1].append(transition)
        if transition.done:
            self._done_episode[env_id][-1] = True
            if len(self._transitions[env_id][-1]) < self._unroll_len:
                logging.warning(
                    'The length of the newest finished episode in node {}, env {}, is {}, which is shorter than unroll_len: {}, and need to be dropped'
                    .format(task.router.node_id, env_id, len(self._transitions[env_id][-1]), self._unroll_len)
                )
                return False
        return True

    def clear(self) -> None:
        for item in self._transitions:
            item.clear()
        for item in self._done_episode:
            item.clear()


def inferencer(cfg: EasyDict, policy: Policy, env: BaseEnvManager) -> Callable:
    """
    Overview:
        The middleware that executes the inference process.
    Arguments:
        - cfg (:obj:`EasyDict`): Config.
        - policy (:obj:`Policy`): The policy to be inferred.
        - env (:obj:`BaseEnvManager`): The env where the inference process is performed. \
            The env.ready_obs (:obj:`tnp.array`) will be used as model input.
    """

    env.seed(cfg.seed)

    def _inference(ctx: "OnlineRLContext"):
        """
        Output of ctx:
            - obs (:obj:`Dict[Tensor]`): The input states fed into the model.
            - action: (:obj:`List[np.ndarray]`): The inferred actions listed by env_id.
            - inference_output (:obj:`Dict[int, Dict]`): The dict that contains env_id (int) \
                and inference result (Dict).
        """

        if env.closed:
            env.launch()

        obs = ttorch.as_tensor(env.ready_obs).to(dtype=ttorch.float32)
        ctx.obs = obs
        # TODO mask necessary rollout

        obs = {i: obs[i] for i in range(obs.shape[0])}  # TBD
        inference_output = policy.forward(obs, **ctx.collect_kwargs)
        ctx.action = [v['action'].numpy() for v in inference_output.values()]  # TBD
        ctx.inference_output = inference_output

    return _inference


def rolloutor(cfg: EasyDict, policy: Policy, env: BaseEnvManager, transitions: TransitionList) -> Callable:
    """
    Overview:
        The middleware that executes the transition process in the env.
    Arguments:
        - cfg (:obj:`EasyDict`): Config.
        - policy (:obj:`Policy`): The policy to be used during transition.
        - env (:obj:`BaseEnvManager`): The env for the collection, the BaseEnvManager object or \
                its derivatives are supported.
        - transitions (:obj:`TransitionList`): The transition information which will be filled \
            in this process, including `obs`, `next_obs`, `action`, `logit`, `value`, `reward` \
            and `done`.
    """

    env_episode_id = [_ for _ in range(env.env_num)]
    current_id = env.env_num

    def _rollout(ctx: "OnlineRLContext"):
        """
        Input of ctx:
            - action: (:obj:`List[np.ndarray]`): The inferred actions from previous inference process.
            - obs (:obj:`Dict[Tensor]`): The states fed into the transition dict.
            - inference_output (:obj:`Dict[int, Dict]`): The inference results to be fed into the \
                transition dict.
            - train_iter (:obj:`int`): The train iteration count to be fed into the transition dict.
            - env_step (:obj:`int`): The count of env step, which will increase by 1 for a single \
                transition call.
            - env_episode (:obj:`int`): The count of env episode, which will increase by 1 if the \
                trajectory stops.
        """

        nonlocal current_id
        timesteps = env.step(ctx.action)
        ctx.env_step += len(timesteps)
        timesteps = [t.tensor() for t in timesteps]
        # TODO abnormal env step
        for i, timestep in enumerate(timesteps):
            transition = policy.process_transition(ctx.obs[i], ctx.inference_output[i], timestep)
            transition = ttorch.as_tensor(transition)  # TBD
            transition.collect_train_iter = ttorch.as_tensor([ctx.train_iter])
            transition.env_data_id = ttorch.as_tensor([env_episode_id[timestep.env_id]])
            transitions.append(timestep.env_id, transition)
            if timestep.done:
                policy.reset([timestep.env_id])
                env_episode_id[timestep.env_id] = current_id
                current_id += 1
                ctx.env_episode += 1
        # TODO log

    return _rollout


def battle_inferencer(cfg: EasyDict, env: BaseEnvManager):

    def _battle_inferencer(ctx: "BattleContext"):
        # Get current env obs.
        obs = env.ready_obs
        # the role of remain_episode is to mask necessary rollouts, avoid processing unnecessary data
        # new_available_env_id = set(obs.keys()).difference(ctx.ready_env_id)
        # ctx.ready_env_id = ctx.ready_env_id.union(set(list(new_available_env_id)[:ctx.remain_episode]))
        # ctx.remain_episode -= min(len(new_available_env_id), ctx.remain_episode)
        # obs = {env_id: obs[env_id] for env_id in ctx.ready_env_id}

        # Policy forward.
        if cfg.transform_obs:
            obs = to_tensor(obs, dtype=torch.float32)
        obs = dicts_to_lists(obs)
        inference_output = [p.forward(obs[i], **ctx.collect_kwargs) for i, p in enumerate(ctx.current_policies)]
        ctx.obs = obs
        ctx.inference_output = inference_output
        # Interact with env.
        actions = {}
        for env_id in range(env.env_num):
            actions[env_id] = []
            for output in inference_output:
                actions[env_id].append(output[env_id]['action'])
        ctx.actions = to_ndarray(actions)

    return _battle_inferencer


def battle_rolloutor(cfg: EasyDict, env: BaseEnvManager, transitions_list: List):

    def _battle_rolloutor(ctx: "BattleContext"):
        timesteps = env.step(ctx.actions)
        ctx.total_envstep_count += len(timesteps)
        ctx.env_step += len(timesteps)
        for env_id, timestep in timesteps.items():
            for policy_id, _ in enumerate(ctx.current_policies):
                policy_timestep_data = [d[policy_id] if not isinstance(d, bool) else d for d in timestep]
                policy_timestep = type(timestep)(*policy_timestep_data)
                transition = ctx.current_policies[policy_id].process_transition(
                    ctx.obs[policy_id][env_id], ctx.inference_output[policy_id][env_id], policy_timestep
                )
                transition = ttorch.as_tensor(transition)
                transition.collect_train_iter = ttorch.as_tensor([ctx.train_iter])
                transitions_list[policy_id].append(env_id, transition)
                if timestep.done:
                    ctx.current_policies[policy_id].reset([env_id])
                    ctx.episode_info[policy_id].append(timestep.info[policy_id])

            if timestep.done:
                # ctx.ready_env_id.remove(env_id)
                ctx.env_episode += 1

    return _battle_rolloutor


def battle_inferencer_for_distar(cfg: EasyDict, env: BaseEnvManager):

    def _battle_inferencer(ctx: "BattleContext"):
        # Get current env obs.
        obs = env.ready_obs
        assert isinstance(obs, dict)

        ctx.obs = obs

        # Policy forward.
        inference_output = {}
        actions = {}
        for env_id in ctx.obs.keys():
            observations = obs[env_id]
            inference_output[env_id] = {}
            actions[env_id] = {}
            for policy_id, policy_obs in observations.items():
                # policy.forward
                output = ctx.current_policies[policy_id].forward(policy_obs)
                inference_output[env_id][policy_id] = output
                actions[env_id][policy_id] = output['action']
        ctx.inference_output = inference_output
        ctx.actions = actions

    return _battle_inferencer


def battle_rolloutor_for_distar(cfg: EasyDict, env: BaseEnvManager, transitions_list: List):

    def _battle_rolloutor(ctx: "BattleContext"):
        timesteps = env.step(ctx.actions)

        ctx.total_envstep_count += len(timesteps)
        ctx.env_step += len(timesteps)

        # for env_id, timestep in timesteps.items():
        # TODO(zms): make sure a standard
        # If for each step, the env manager can't get the obs of all envs, we need to use dict here.
        for env_id, timestep in enumerate(timesteps):
            if timestep.info.get('abnormal'):
                # TODO(zms): cannot get exact env_step of a episode because for each observation,
                # in most cases only one of two policies has a obs.
                # ctx.total_envstep_count -= transitions_list[0].length(env_id)
                # ctx.env_step -= transitions_list[0].length(env_id)

                # 1st case when env step has bug and need to reset.
                for policy_id, _ in enumerate(ctx.current_policies):
                    transitions_list[policy_id].clear_newest_episode(env_id)
                    ctx.current_policies[policy_id].reset([env_id])
                continue

            append_succeed = True
            for policy_id, _ in enumerate(ctx.current_policies):
                transition = ctx.current_policies[policy_id].process_transition(timestep)
                transition = EasyDict(transition)
                transition.collect_train_iter = ttorch.as_tensor([ctx.train_iter])

                # 2nd case when the number of transitions in one of all the episodes is shorter than unroll_len
                append_succeed = append_succeed and transitions_list[policy_id].append(env_id, transition)
                if timestep.done:
                    ctx.current_policies[policy_id].reset([env_id])
                    ctx.episode_info[policy_id].append(timestep.info[policy_id])

            if not append_succeed:
                for policy_id, _ in enumerate(ctx.current_policies):
                    transitions_list[policy_id].clear_newest_episode(env_id)
                    ctx.episode_info[policy_id].pop()
            elif timestep.done:
                ctx.env_episode += 1

    return _battle_rolloutor
