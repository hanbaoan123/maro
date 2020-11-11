# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from typing import Callable

import numpy as np
import torch
import torch.nn as nn

from maro.rl.algorithms.abs_algorithm import AbsAlgorithm
from maro.rl.utils.trajectory_utils import get_lambda_returns


class PPOConfig:
    """Configuration for the Proximal Policy Optimization (PPO) algorithm.

    Args:
        num_actions (int): number of possible actions
        reward_decay (float): reward decay as defined in standard RL terminology
        clip_ratio (float): clip ratio as defined in PPO's objective function.
        policy_train_iters (int): number of gradient descent steps for the policy model per call to ``train``.
        value_train_iters (int): number of gradient descent steps for the value model per call to ``train``.
        k (int): number of time steps used in computing returns or return estimates. Defaults to -1, in which case
            rewards are accumulated until the end of the trajectory.
        lam (float): lambda coefficient used in computing lambda returns. Defaults to 1.0, in which case the usual
            k-step return is computed.
    """
    __slots__ = ["num_actions", "reward_decay", "clip_ratio", "policy_train_iters", "value_train_iters", "k", "lam"]

    def __init__(
        self, num_actions: int, reward_decay: float, clip_ratio: float, policy_train_iters: int,
        value_train_iters: int, k: int = -1, lam: float = 1.0
    ):
        self.num_actions = num_actions
        self.reward_decay = reward_decay
        self.clip_ratio = clip_ratio
        self.policy_train_iters = policy_train_iters
        self.value_train_iters = value_train_iters
        self.k = k
        self.lam = lam


class PPO(AbsAlgorithm):
    """Proximal Policy Optimization (PPO) algorithm.

    See https://arxiv.org/pdf/1707.06347.pdf for details.

    Args:
        policy_model (nn.Module): model for generating actions given states.
        value_model (nn.Module): model for estimating state values.
        value_loss_func (Callable): loss function for the value model.
        policy_optimizer_cls: torch optimizer class for the policy model. If this is None, the policy model is not
            trainable.
        policy_optimizer_params: parameters required for the policy optimizer class.
        value_optimizer_cls: torch optimizer class for the value model. If this is None, the value model is not
            trainable.
        value_optimizer_params: parameters required for the value optimizer class.
        hyper_params: hyper-parameter set for the AC algorithm.
    """

    def __init__(
        self, policy_model: nn.Module, value_model: nn.Module, value_loss_func: Callable, policy_optimizer_cls,
        policy_optimizer_params, value_optimizer_cls, value_optimizer_params, hyper_params: PPOConfig
    ):
        super().__init__()
        self._device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self._model_dict = {"policy": policy_model.to(self._device), "value": value_model.to(self._device)}
        self._policy_optimizer = policy_optimizer_cls(
            self._model_dict["policy"].parameters(), **policy_optimizer_params
        )
        self._value_optimizer = value_optimizer_cls(
            self._model_dict["value"].parameters(), **value_optimizer_params
        )
        self._value_loss_func = value_loss_func
        self._hyper_params = hyper_params

    def choose_action(self, state: np.ndarray, epsilon: float = None):
        state = torch.from_numpy(state).unsqueeze(0).to(self._device)   # (1, state_dim)
        self._model_dict["policy"].eval()
        with torch.no_grad():
            action_dist = self._model_dict["policy"](state).squeeze().numpy()  # (num_actions,)
        return np.random.choice(self._hyper_params.num_actions, p=action_dist)

    def _get_values_and_bootstrapped_returns(self, states: torch.tensor, rewards: np.ndarray):
        state_values = self._model_dict["value"](states).detach().squeeze()
        state_values_numpy = state_values.numpy()
        return_est = get_lambda_returns(
            rewards, state_values_numpy, self._hyper_params.reward_decay, self._hyper_params.lam,
            k=self._hyper_params.k
        )
        return_est = torch.from_numpy(return_est)
        return state_values, return_est

    def train(
        self, states: np.ndarray, actions: np.ndarray, log_action_prob: np.ndarray, rewards: np.ndarray
    ):
        if not hasattr(self, "_policy_optimizer") and not hasattr(self, "_value_optimizer"):
            return

        states = torch.from_numpy(states).to(self._device)  # (N, state_dim)
        state_values, return_est = self._get_values_and_bootstrapped_returns(states, rewards)
        advantages = return_est - state_values
        actions = torch.from_numpy(actions).to(self._device)  # (N,)
        log_action_prob_old = torch.from_numpy(log_action_prob).to(self._device)

        # policy model training (with the value model fixed)
        if hasattr(self, "_policy_optimizer"):
            for _ in range(self._hyper_params.policy_train_iters):
                action_prob = self._model_dict["policy"](states).gather(1, actions.unsqueeze(1)).squeeze()  # (N, 1)
                ratio = torch.exp(torch.log(action_prob) - log_action_prob_old)
                clipped_ratio = torch.clamp(ratio, 1 - self._hyper_params.clip_ratio, 1 + self._hyper_params.clip_ratio)
                loss = -(torch.min(ratio * advantages, clipped_ratio * advantages)).mean()
                self._policy_optimizer.zero_grad()
                loss.backward()
                self._policy_optimizer.step()

        # value model training
        if hasattr(self, "_value_optimizer"):
            for _ in range(self._hyper_params.value_train_iters):
                value_loss = self._value_loss_func(self._model_dict["value"](states), return_est)
                self._value_optimizer.zero_grad()
                value_loss.backward()
                self._value_optimizer.step()