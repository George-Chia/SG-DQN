import logging
import torch
import numpy as np
from numpy.linalg import norm
import itertools
from crowd_sim.envs.policy.policy import Policy
from crowd_sim.envs.utils.action import ActionRot, ActionXY
from crowd_sim.envs.utils.state import tensor_to_joint_state
from crowd_sim.envs.utils.utils import point_to_segment_dist
from crowd_nav.policy.state_predictor import StatePredictor, LinearStatePredictor
from crowd_nav.policy.graph_model import RGL
from crowd_nav.policy.value_estimator import DQNNetwork


class TreeSearchRL(Policy):
    def __init__(self):
        super().__init__()
        self.name = 'TreeSearchRL'
        self.trainable = True
        self.multiagent_training = True
        self.kinematics = None
        self.epsilon = None
        self.gamma = None
        self.sampling = None
        self.speed_samples = None
        self.rotation_samples = None
        self.action_space = None
        self.rotation_constraint = None
        self.speeds = None
        self.rotations = None
        self.action_values = None
        self.robot_state_dim = 9
        self.human_state_dim = 5
        self.v_pref = 1
        self.share_graph_model = None
        self.value_estimator = None
        self.linear_state_predictor = None
        self.state_predictor = None
        self.planning_depth = None
        self.planning_width = None
        self.do_action_clip = None
        self.sparse_search = None
        self.sparse_speed_samples = 2
        self.sparse_rotation_samples = 8
        self.action_group_index = []
        self.traj = None
        self.count=0

    def configure(self, config, device):
        self.set_common_parameters(config)
        self.planning_depth = config.model_predictive_rl.planning_depth
        self.do_action_clip = config.model_predictive_rl.do_action_clip
        if hasattr(config.model_predictive_rl, 'sparse_search'):
            self.sparse_search = config.model_predictive_rl.sparse_search
        self.planning_width = config.model_predictive_rl.planning_width
        self.share_graph_model = config.model_predictive_rl.share_graph_model
        self.linear_state_predictor = config.model_predictive_rl.linear_state_predictor
        # self.set_device(device)
        self.device = device


        if self.linear_state_predictor:
            self.state_predictor = LinearStatePredictor(config, self.time_step)
            graph_model = RGL(config, self.robot_state_dim, self.human_state_dim)
            self.value_estimator = DQNNetwork(config, graph_model)
            self.model = [graph_model, self.value_estimator.value_network]
        else:
            if self.share_graph_model:
                graph_model = RGL(config, self.robot_state_dim, self.human_state_dim)
                self.value_estimator = DQNNetwork(config, graph_model)
                self.state_predictor = StatePredictor(config, graph_model, self.time_step)
                self.model = [graph_model, self.value_estimator.value_network, self.state_predictor.human_motion_predictor]
            else:
                graph_model1 = RGL(config, self.robot_state_dim, self.human_state_dim)
                self.value_estimator = DQNNetwork(config, graph_model1)
                graph_model2 = RGL(config, self.robot_state_dim, self.human_state_dim)
                self.state_predictor = StatePredictor(config, graph_model2, self.time_step)
                self.model = [graph_model1, graph_model2, self.value_estimator.value_network,
                              self.state_predictor.human_motion_predictor]

        logging.info('Planning depth: {}'.format(self.planning_depth))
        logging.info('Planning width: {}'.format(self.planning_width))
        logging.info('Sparse search: {}'.format(self.sparse_search))

        if self.planning_depth > 1 and not self.do_action_clip:
            logging.warning('Performing d-step planning without action space clipping!')

    def set_common_parameters(self, config):
        self.gamma = config.rl.gamma
        self.kinematics = config.action_space.kinematics
        self.sampling = config.action_space.sampling
        self.speed_samples = config.action_space.speed_samples
        self.rotation_samples = config.action_space.rotation_samples
        self.rotation_constraint = config.action_space.rotation_constraint

    def set_device(self, device):
        self.device = device
        for model in self.model:
            model.to(device)

    def set_epsilon(self, epsilon):
        self.epsilon = epsilon

    def set_time_step(self, time_step):
        self.time_step = time_step
        self.state_predictor.time_step = time_step

    def get_normalized_gamma(self):
        return 0.95
        return pow(self.gamma, self.time_step * self.v_pref)

    def get_model(self):
        return self.value_estimator

    def get_state_dict(self):
        if self.state_predictor.trainable:
            if self.share_graph_model:
                return {
                    'graph_model': self.value_estimator.graph_model.state_dict(),
                    'value_network': self.value_estimator.value_network.state_dict(),
                    'motion_predictor': self.state_predictor.human_motion_predictor.state_dict()
                }
            else:
                return {
                    'graph_model1': self.value_estimator.graph_model.state_dict(),
                    'graph_model2': self.state_predictor.graph_model.state_dict(),
                    'value_network': self.value_estimator.value_network.state_dict(),
                    'motion_predictor': self.state_predictor.human_motion_predictor.state_dict()
                }
        else:
            return {
                    'graph_model': self.value_estimator.graph_model.state_dict(),
                    'value_network': self.value_estimator.value_network.state_dict()
                }

    def get_traj(self):
        return self.traj

    def load_state_dict(self, state_dict):
        if self.state_predictor.trainable:
            if self.share_graph_model:
                self.value_estimator.graph_model.load_state_dict(state_dict['graph_model'])
            else:
                self.value_estimator.graph_model.load_state_dict(state_dict['graph_model1'])
                self.state_predictor.graph_model.load_state_dict(state_dict['graph_model2'])

            self.value_estimator.value_network.load_state_dict(state_dict['value_network'])
            self.state_predictor.human_motion_predictor.load_state_dict(state_dict['motion_predictor'])
        else:
            self.value_estimator.graph_model.load_state_dict(state_dict['graph_model'])
            self.value_estimator.value_network.load_state_dict(state_dict['value_network'])

    def save_model(self, file):
        torch.save(self.get_state_dict(), file)

    def load_model(self, file):
        checkpoint = torch.load(file)
        self.load_state_dict(checkpoint)

    def build_action_space(self, v_pref):
        """
        Action space consists of 25 uniformly sampled actions in permitted range and 25 randomly sampled actions.
        """
        holonomic = True if self.kinematics == 'holonomic' else False
        # speeds = [(np.exp((i + 1) / self.speed_samples) - 1) / (np.e - 1) * v_pref for i in range(self.speed_samples)]
        speeds = [(i+1)/self.speed_samples * v_pref for i in range(self.speed_samples)]
        if holonomic:
            rotations = np.linspace(0, 2 * np.pi, self.rotation_samples, endpoint=False)
        else:
            rotations = np.linspace(-self.rotation_constraint, self.rotation_constraint, self.rotation_samples)

        action_space = [ActionXY(0, 0) if holonomic else ActionRot(0, 0)]
        self.action_group_index.append(0)
        for j, speed in enumerate(speeds):
            for i, rotation in enumerate(rotations):
                action_index = j * self.rotation_samples + i + 1
                self.action_group_index.append(action_index)
                if holonomic:
                    action_space.append(ActionXY(speed * np.cos(rotation), speed * np.sin(rotation)))
                else:
                    action_space.append(ActionRot(speed, rotation))
        self.speeds = speeds
        self.rotations = rotations
        self.action_space = action_space

    def predict(self, state):
        """
        A base class for all methods that takes pairwise joint state as input to value network.
        The input to the value network is always of shape (batch_size, # humans, rotated joint state length)

        """
        self.count=self.count+1
        if self.phase is None or self.device is None:
            raise AttributeError('Phase, device attributes have to be set!')
        if self.phase == 'train' and self.epsilon is None:
            raise AttributeError('Epsilon attribute has to be set in training phase')

        if self.reach_destination(state):
            return ActionXY(0, 0) if self.kinematics == 'holonomic' else ActionRot(0, 0)
        if self.action_space is None:
            self.build_action_space(1.0)
        max_action = None
        origin_max_value = float('-inf')
        state_tensor = state.to_tensor(add_batch_size=True, device=self.device)
        probability = np.random.random()
        if self.phase == 'train' and probability < self.epsilon:
            max_action_index = np.random.choice(len(self.action_space))
            max_action = self.action_space[max_action_index]
            self.last_state = self.transform(state)
            return max_action, max_action_index
        else:
            max_value, max_action_index, max_traj = self.V_planning(state_tensor, self.planning_depth, self.planning_width)
            if max_value[0] > origin_max_value:
                max_action = self.action_space[max_action_index[0]]
            if max_action is None:
                raise ValueError('Value network is not well trained.')

        if self.phase == 'train':
            self.last_state = self.transform(state)
        else:
            self.last_state = self.transform(state)
            self.traj = max_traj[0]
        return max_action, int(max_action_index[0])

    def V_planning(self, state, depth, width):
        """ Plans n steps into future based on state action value function. Computes the value for the current state as well as the trajectories
        defined as a list of (state, action, reward) triples
        """
        # current_state_value = self.value_estimator(state)
        robot_state_batch = state[0]
        human_state_batch = state[1]
        if depth == 0:
            q_value = torch.Tensor(self.value_estimator(state))
            max_action_value, max_action_indexes = torch.max(q_value, dim=1)
            trajs = []
            for i in range(robot_state_batch.shape[0]):
                cur_state = (robot_state_batch[i, :, :].unsqueeze(0), human_state_batch[i, :, :].unsqueeze(0))
                trajs.append([(cur_state, None, None)])
            return max_action_value, max_action_indexes, trajs
        else:
            q_value = torch.Tensor(self.value_estimator(state))
            max_action_value, max_action_indexes = torch.topk(q_value, width, dim=1)
        action_stay = []
        for i in range(robot_state_batch.shape[0]):
            action_stay.append(ActionXY(0, 0))
        _, pre_next_state = self.state_predictor(state, action_stay)
        next_robot_state_batch = None
        next_human_state_batch = None
        reward_est = torch.zeros(state[0].shape[0], width) * float('inf')

        for i in range(robot_state_batch.shape[0]):
            cur_state = (robot_state_batch[i, :, :].unsqueeze(0), human_state_batch[i, :, :].unsqueeze(0))
            next_human_state = pre_next_state[i, :, :].unsqueeze(0)
            for j in range(width):
                cur_action = self.action_space[max_action_indexes[i][j]]
                next_robot_state = self.compute_next_robot_state(cur_state[0], cur_action)
                if next_robot_state_batch is None:
                    next_robot_state_batch = next_robot_state
                    next_human_state_batch = next_human_state
                else:
                    next_robot_state_batch = torch.cat((next_robot_state_batch, next_robot_state), dim=0)
                    next_human_state_batch = torch.cat((next_human_state_batch, next_human_state), dim=0)
                reward_est[i][j] = self.estimate_reward_on_predictor(
                    tensor_to_joint_state(cur_state), tensor_to_joint_state((next_robot_state, next_human_state)))
        next_state_batch = (next_robot_state_batch, next_human_state_batch)
        if self.planning_depth - depth >= 2 and self.planning_depth > 2:
            cur_width = 1
        else:
            cur_width = int(self.planning_width/2)
        next_values, next_action_indexes, next_trajs = self.V_planning(next_state_batch, depth-1, cur_width)
        next_values = next_values.view(state[0].shape[0], width)
        returns = (reward_est + self.get_normalized_gamma()*next_values + max_action_value) / (depth + 1)

        max_action_return, max_action_index = torch.max(returns, dim=1)
        trajs = []
        max_returns = []
        max_actions = []
        for i in range(robot_state_batch.shape[0]):
            cur_state = (robot_state_batch[i, :, :].unsqueeze(0), human_state_batch[i, :, :].unsqueeze(0))
            action_id = max_action_index[i]
            trajs_id = i * width + action_id
            action = max_action_indexes[i][action_id]
            next_traj = next_trajs[trajs_id]
            trajs.append([(cur_state, action, reward_est)] + next_traj)
            max_returns.append(max_action_return[i].data)
            max_actions.append(action)
        max_returns = torch.tensor(max_returns)
        return max_returns, max_actions, trajs

    def estimate_reward_on_predictor(self, state, next_state):
        """ If the time step is small enough, it's okay to model agent as linear movement during this period

        """
        # collision detection
        if isinstance(state, list) or isinstance(state, tuple):
            state = tensor_to_joint_state(state)
        human_states = state.human_states
        robot_state = state.robot_state

        next_robot_state = next_state.robot_state
        next_human_states = next_state.human_states

        # action_vel_length = np.sqrt(next_robot_state.vx*next_robot_state.vx + next_robot_state.vy*next_robot_state.vy)
        # robot_vel_length = np.sqrt(robot_state.vx*robot_state.vx + robot_state.vy*robot_state.vy)
        # vel_dot = next_robot_state.vx*robot_state.vx + next_robot_state.vy*robot_state.vy
        # delta_w = vel_dot/action_vel_length/robot_vel_length
        # delta_w = 0.0
        # if delta_w < 0.5:
        #     reward_omega = -0.01 * (0.5 - delta_w) * (0.5 - delta_w)
        # else:
        #     reward_omega = 0.0
        cur_position = np.array((robot_state.px, robot_state.py))
        end_position = np.array((next_robot_state.px, next_robot_state.py))
        goal_position = np.array((robot_state.gx, robot_state.gy))
        reward_goal = 0.01 * (norm(cur_position - goal_position) - norm(end_position - goal_position))
        # check if reaching the goal
        reaching_goal = norm(end_position - np.array([robot_state.gx, robot_state.gy])) < robot_state.radius
        dmin = float('inf')
        collision = False
        for i, human in enumerate(human_states):
            next_human = next_human_states[i]
            px = human.px - robot_state.px
            py = human.py - robot_state.py
            ex = next_human.px - next_robot_state.px
            ey = next_human.py - next_robot_state.py
            # closest distance between boundaries of two agents
            closest_dist = point_to_segment_dist(px, py, ex, ey, 0, 0) - human.radius - robot_state.radius
            if closest_dist < 0:
                collision = True
                break
            elif closest_dist < dmin:
                dmin = closest_dist
        if collision:
            reward = -0.25
        elif reaching_goal:
            reward = 1
        elif dmin < 0.2:
            # adjust the reward based on FPS
            reward = (dmin - 0.2) * 0.25
            # self.time_step * 0.5
        else:
            reward = 0
        reward = reward + reward_goal - 0.005
        if collision:
            reward = reward - 100
        reward = reward * 10
        return reward

    def transform(self, state):
        """
        Take the JointState to tensors

        :param state:
        :return: tensor of shape (# of agent, len(state))
        """
        robot_state_tensor = torch.Tensor([state.robot_state.to_tuple()]).to(self.device)
        human_states_tensor = torch.Tensor([human_state.to_tuple() for human_state in state.human_states]). \
            to(self.device)

        return robot_state_tensor, human_states_tensor

    def compute_next_robot_state(self, robot_state, action):
        if robot_state.shape[0] != 1:
            raise NotImplementedError
        next_state = robot_state.clone().squeeze()
        if self.kinematics == 'holonomic':
            next_state[0] = next_state[0] + action.vx * self.time_step
            next_state[1] = next_state[1] + action.vy * self.time_step
            next_state[2] = action.vx
            next_state[3] = action.vy
        else:
            next_state[7] = next_state[7] + action.r
            next_state[0] = next_state[0] + np.cos(next_state[7]) * action.v * self.time_step
            next_state[1] = next_state[1] + np.sin(next_state[7]) * action.v * self.time_step
            next_state[2] = np.cos(next_state[7]) * action.v
            next_state[3] = np.sin(next_state[7]) * action.v
        return next_state.unsqueeze(0).unsqueeze(0)