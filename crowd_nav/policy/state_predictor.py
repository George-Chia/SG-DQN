import torch
import torch.nn as nn
import numpy as np
from crowd_nav.policy.helpers import mlp


class StatePredictor(nn.Module):
    def __init__(self, config, graph_model, human_state_dim):
        """
        This function predicts the next state given the current state as input.
        It uses a graph model to encode the state into a latent space and predict each human's next state.
        """
        super().__init__()
        self.kinematics = config.action_space.kinematics
        self.time_step = 0.25
        self.graph_model = graph_model
        self.human_motion_predictor = mlp(config.gcn.X_dim, config.model_predictive_rl.motion_predictor_dims)

    def forward(self, state, action, time_step=0.25):
        """ Predict the next state tensor given current state as input.

        :return: tensor of shape (batch_size, # of agents, feature_size)
        """
        assert len(state[0].shape) == 3
        assert len(state[1].shape) == 3

        state_embedding = self.graph_model(state)
        # extract the robot state
        if action is None:
            next_robot_state = None
        else:
            # only for training the human motion predictor
            next_robot_state = self.compute_next_state(state[0], action)
        next_human_states = self.human_motion_predictor(state_embedding)[:, 1:, :]

        next_observation = [next_robot_state, next_human_states]
        return next_observation

    def compute_next_state(self, robot_state, action):
        # currently it can not perform parallel computation
        assert robot_state.shape[0] == 1

        # px, py, vx, vy, radius, gx, gy, v_pref, theta
        next_state = robot_state.clone().squeeze()
        if self.kinematics == 'holonomic':
            raise NotImplementedError
        else:
            next_state[7] = next_state[7] + action.r
            next_state[0] = next_state[0] + np.cos(next_state[7]) * action.v * self.time_step
            next_state[1] = next_state[1] + np.sin(next_state[7]) * action.v * self.time_step
            next_state[2] = np.cos(next_state[7]) * action.v
            next_state[3] = np.sin(next_state[7]) * action.v

        return next_state.unsqueeze(0).unsqueeze(0)

