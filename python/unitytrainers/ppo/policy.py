import logging

import numpy as np
import tensorflow as tf
from unitytrainers.ppo.models import PPOModel
from unitytrainers.policy import Policy

logger = logging.getLogger("unityagents")


class PPOPolicy(Policy):
    def __init__(self, seed, brain, trainer_parameters, sess, is_training):
        super().__init__(seed, brain, trainer_parameters, sess)
        self.has_updated = False
        self.use_curiosity = bool(trainer_parameters['use_curiosity'])
        with tf.variable_scope(self.variable_scope):
            tf.set_random_seed(seed)
            self.model = PPOModel(brain,
                                  lr=float(trainer_parameters['learning_rate']),
                                  h_size=int(trainer_parameters['hidden_units']),
                                  epsilon=float(trainer_parameters['epsilon']),
                                  beta=float(trainer_parameters['beta']),
                                  max_step=float(trainer_parameters['max_steps']),
                                  normalize=trainer_parameters['normalize'],
                                  use_recurrent=trainer_parameters['use_recurrent'],
                                  num_layers=int(trainer_parameters['num_layers']),
                                  m_size=self.m_size,
                                  use_curiosity=bool(trainer_parameters['use_curiosity']),
                                  curiosity_strength=float(trainer_parameters['curiosity_strength']),
                                  curiosity_enc_size=float(trainer_parameters['curiosity_enc_size']))

        self.inference_dict = {'action': self.model.output, 'log_probs': self.model.all_log_probs,
                               'value': self.model.value, 'entropy': self.model.entropy, 'learning_rate':
                                   self.model.learning_rate}
        if self.use_continuous_act:
            self.inference_dict['pre_action'] = self.model.output_pre
        if self.use_recurrent:
            self.inference_dict['memory_out'] = self.model.memory_out
        if is_training and self.use_vector_obs and trainer_parameters['normalize']:
            self.inference_dict['update_mean'] = self.model.update_mean
            self.inference_dict['update_variance'] = self.model.update_variance

        self.update_dict = {'value_loss': self.model.value_loss,
                            'policy_loss': self.model.policy_loss,
                            'update_batch': self.model.update_batch}
        if self.use_curiosity:
            self.update_dict['forward_loss'] = self.model.forward_loss
            self.update_dict['inverse_loss'] = self.model.inverse_loss

    def act(self, curr_brain_info):
        feed_dict = {self.model.batch_size: len(curr_brain_info.vector_observations),
                     self.model.sequence_length: 1}
        if self.use_recurrent:
            if not self.use_continuous_act:
                feed_dict[self.model.prev_action] = curr_brain_info.previous_vector_actions.reshape(
                    [-1, len(self.model.a_size)])
            if curr_brain_info.memories.shape[1] == 0:
                curr_brain_info.memories = np.zeros((len(curr_brain_info.agents), self.m_size))
            feed_dict[self.model.memory_in] = curr_brain_info.memories
        if self.use_visual_obs:
            for i, _ in enumerate(curr_brain_info.visual_observations):
                feed_dict[self.model.visual_in[i]] = curr_brain_info.visual_observations[i]
        if self.use_vector_obs:
            feed_dict[self.model.vector_in] = curr_brain_info.vector_observations

        network_output = self.sess.run(list(self.inference_dict.values()), feed_dict=feed_dict)
        run_out = dict(zip(list(self.inference_dict.keys()), network_output))
        return run_out

    def update(self, buffer, n_sequences, i):
        start = i * n_sequences
        end = (i + 1) * n_sequences
        feed_dict = {self.model.batch_size: n_sequences,
                     self.model.sequence_length: self.sequence_length,
                     self.model.mask_input: np.array(buffer['masks'][start:end]).flatten(),
                     self.model.returns_holder: np.array(buffer['discounted_returns'][start:end]).flatten(),
                     self.model.old_value: np.array(buffer['value_estimates'][start:end]).flatten(),
                     self.model.advantage: np.array(buffer['advantages'][start:end]).reshape([-1, 1]),
                     self.model.all_old_log_probs: np.array(buffer['action_probs'][start:end]).reshape(
                         [-1, sum(self.model.a_size)])}
        if self.use_continuous_act:
            feed_dict[self.model.output_pre] = np.array(buffer['actions_pre'][start:end]).reshape(
                [-1, self.model.a_size[0]])
        else:
            feed_dict[self.model.action_holder] = np.array(buffer['actions'][start:end]).reshape(
                [-1, len(self.model.a_size)])
            if self.use_recurrent:
                feed_dict[self.model.prev_action] = np.array(buffer['prev_action'][start:end]).reshape(
                    [-1, len(self.model.a_size)])
        if self.use_vector_obs:
            total_observation_length = self.model.o_size
            feed_dict[self.model.vector_in] = np.array(buffer['vector_obs'][start:end]).reshape(
                [-1, total_observation_length])
            if self.use_curiosity:
                feed_dict[self.model.next_vector_in] = np.array(buffer['next_vector_in'][start:end]) \
                    .reshape([-1, total_observation_length])
        if self.use_visual_obs:
            for i, _ in enumerate(self.model.visual_in):
                _obs = np.array(buffer['visual_obs%d' % i][start:end])
                if self.sequence_length > 1 and self.use_recurrent:
                    (_batch, _seq, _w, _h, _c) = _obs.shape
                    feed_dict[self.model.visual_in[i]] = _obs.reshape([-1, _w, _h, _c])
                else:
                    feed_dict[self.model.visual_in[i]] = _obs
            if self.use_curiosity:
                for i, _ in enumerate(self.model.visual_in):
                    _obs = np.array(buffer['next_visual_obs%d' % i][start:end])
                    if self.sequence_length > 1 and self.use_recurrent:
                        (_batch, _seq, _w, _h, _c) = _obs.shape
                        feed_dict[self.model.next_visual_in[i]] = _obs.reshape([-1, _w, _h, _c])
                    else:
                        feed_dict[self.model.next_visual_in[i]] = _obs
        if self.use_recurrent:
            mem_in = np.array(buffer['memory'][start:end])[:, 0, :]
            feed_dict[self.model.memory_in] = mem_in
        self.has_updated = True
        network_out = self.sess.run(list(self.update_dict.values()), feed_dict=feed_dict)
        run_out = dict(zip(list(self.update_dict.keys()), network_out))
        return run_out

    def get_intrinsic_rewards(self, curr_info, next_info):
        """
        Generates intrinsic reward used for Curiosity-based training.
        :BrainInfo curr_info: Current BrainInfo.
        :BrainInfo next_info: Next BrainInfo.
        :return: Intrinsic rewards for all agents.
        """
        if self.use_curiosity:
            feed_dict = {self.model.batch_size: len(next_info.vector_observations),
                         self.model.sequence_length: 1}
            if self.use_continuous_act:
                feed_dict[self.model.output] = next_info.previous_vector_actions
            else:
                feed_dict[self.model.action_holder] = next_info.previous_vector_actions

            if self.use_visual_obs:
                for i in range(len(curr_info.visual_observations)):
                    feed_dict[self.model.visual_in[i]] = curr_info.visual_observations[i]
                    feed_dict[self.model.next_visual_in[i]] = next_info.visual_observations[i]
            if self.use_vector_obs:
                feed_dict[self.model.vector_in] = curr_info.vector_observations
                feed_dict[self.model.next_vector_in] = next_info.vector_observations
            if self.use_recurrent:
                if curr_info.memories.shape[1] == 0:
                    curr_info.memories = np.zeros((len(curr_info.agents), self.m_size))
                feed_dict[self.model.memory_in] = curr_info.memories
            intrinsic_rewards = self.sess.run(self.model.intrinsic_reward,
                                              feed_dict=feed_dict) * float(self.has_updated)
            return intrinsic_rewards
        else:
            return None

    def get_value_estimate(self, brain_info, idx):
        """
        Generates value estimates for bootstrapping.
        :param brain_info: BrainInfo to be used for bootstrapping.
        :param idx: Index in BrainInfo of agent.
        :return: Value estimate.
        """
        feed_dict = {self.model.batch_size: 1, self.model.sequence_length: 1}
        if self.use_visual_obs:
            for i in range(len(brain_info.visual_observations)):
                feed_dict[self.model.visual_in[i]] = [brain_info.visual_observations[i][idx]]
        if self.use_vector_obs:
            feed_dict[self.model.vector_in] = [brain_info.vector_observations[idx]]
        if self.use_recurrent:
            if brain_info.memories.shape[1] == 0:
                brain_info.memories = np.zeros(
                    (len(brain_info.vector_observations), self.m_size))
            feed_dict[self.model.memory_in] = [brain_info.memories[idx]]
        if not self.use_continuous_act and self.use_recurrent:
            feed_dict[self.model.prev_action] = brain_info.previous_vector_actions[idx].reshape(
                [-1, len(self.model.a_size)])
        value_estimate = self.sess.run(self.model.value, feed_dict)
        return value_estimate

    @property
    def graph_scope(self):
        """
        Returns the graph scope of the trainer.
        """
        return self.variable_scope

    def get_last_reward(self):
        """
        Returns the last reward the trainer has had
        :return: the new last reward
        """
        return self.sess.run(self.model.last_reward)

    def get_inference_vars(self):
        return list(self.inference_dict.keys())

    def get_update_vars(self):
        return list(self.update_dict.keys())

    def get_current_step(self):
        step = self.sess.run(self.model.global_step)
        return step

    def increment_step(self):
        self.sess.run(self.model.increment_step)

    def update_reward(self, new_reward):
        self.sess.run(self.model.update_reward,
                      feed_dict={self.model.new_reward: new_reward})
