# Copyright 2018 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""NCF framework to train and evaluate the NeuMF model.

The NeuMF model assembles both MF and MLP models under the NCF framework. Check
`neumf_model.py` for more details about the models.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import contextlib
import heapq
import math
import multiprocessing
import os
import signal
import typing

# pylint: disable=g-bad-import-order
import numpy as np
from absl import app as absl_app
from absl import flags
import tensorflow as tf
# pylint: enable=g-bad-import-order

from official.datasets import movielens
from official.recommendation import neumf_model
from official.recommendation.data_server import pipeline
from official.recommendation.data_server import prepare
from official.utils.flags import core as flags_core
from official.utils.logs import hooks_helper
from official.utils.logs import logger
from official.utils.misc import distribution_utils
from official.utils.misc import model_helpers

_TOP_K = 10  # Top-k list for evaluation
# keys for evaluation metrics
_HR_KEY = "HR"
_NDCG_KEY = "NDCG"


def evaluate_model(estimator, ncf_dataset, pred_input_fn):
  # type: (tf.estimator.Estimator, prepare.NCFDataset, typing.Callable) -> dict
  """Model evaluation with HR and NDCG metrics.

  The evaluation protocol is to rank the test interacted item (truth items)
  among the randomly chosen 999 items that are not interacted by the user.
  The performance of the ranked list is judged by Hit Ratio (HR) and Normalized
  Discounted Cumulative Gain (NDCG).

  For evaluation, the ranked list is truncated at 10 for both metrics. As such,
  the HR intuitively measures whether the test item is present on the top-10
  list, and the NDCG accounts for the position of the hit by assigning higher
  scores to hits at top ranks. Both metrics are calculated for each test user,
  and the average scores are reported.

  Args:
    estimator: The Estimator.
    ncf_dataset: An NCFDataSet object, which contains the information about
      test/eval dataset, such as:
        num_users: How many unique users are in the eval set.
        test_data: The points which are used for consistent evaluation. These
          are already included in the pred_input_fn.
    pred_input_fn: The input function for the test data.

  Returns:
    eval_results: A dict of evaluation results for benchmark logging.
      eval_results = {
        _HR_KEY: hr,
        _NDCG_KEY: ndcg,
        tf.GraphKeys.GLOBAL_STEP: global_step
      }
      where hr is an integer indicating the average HR scores across all users,
      ndcg is an integer representing the average NDCG scores across all users,
      and global_step is the global step
  """

  tf.logging.info("Computing predictions for eval set...")

  # Get predictions
  predictions = estimator.predict(input_fn=pred_input_fn,
                                  yield_single_examples=False)
  prediction_batches = [p[movielens.RATING_COLUMN] for p in predictions]

  # Reshape the predicted scores and each user takes one row
  predicted_scores_by_user = np.concatenate(prediction_batches, axis=0).reshape(
      ncf_dataset.num_users, -1)

  tf.logging.info("Computing metrics...")

  # NumPy has an np.argparition() method, however log(1000) is so small that
  # sorting the whole array is simpler and fast enough.
  top_indicies = np.argsort(predicted_scores_by_user, axis=1)[:, -_TOP_K:]
  top_indicies = np.flip(top_indicies, axis=1)

  # Both HR and NDCG vectorized computation takes advantage of the fact that if
  # the positive example for a user is not in the top k, that index does not
  # appear. That is to say:   hit_indicies.shape[0] <= num_users
  hit_indicies = np.argwhere(np.equal(top_indicies, 0))
  hr = hit_indicies.shape[0] / ncf_dataset.num_users
  ndcg = np.sum(np.log(2) / np.log(hit_indicies[:, 1] + 2)) / ncf_dataset.num_users

  global_step = estimator.get_variable_value(tf.GraphKeys.GLOBAL_STEP)
  eval_results = {
      _HR_KEY: hr,
      _NDCG_KEY: ndcg,
      tf.GraphKeys.GLOBAL_STEP: global_step
  }

  return eval_results


def convert_keras_to_estimator(keras_model, num_gpus, model_dir):
  """Configure and convert keras model to Estimator.

  Args:
    keras_model: A Keras model object.
    num_gpus: An integer, the number of gpus.
    model_dir: A string, the directory to save and restore checkpoints.

  Returns:
    est_model: The converted Estimator.
  """
  optimizer = tf.train.AdamOptimizer(
      learning_rate=FLAGS.learning_rate)
  keras_model.compile(optimizer=optimizer, loss="binary_crossentropy")

  if num_gpus == 0:
    distribution = tf.contrib.distribute.OneDeviceStrategy("device:CPU:0")
  elif num_gpus == 1:
    distribution = tf.contrib.distribute.OneDeviceStrategy("device:GPU:0")
  else:
    distribution = tf.contrib.distribute.MirroredStrategy(num_gpus=num_gpus)

  run_config = tf.estimator.RunConfig(train_distribute=distribution)

  estimator = tf.keras.estimator.model_to_estimator(
      keras_model=keras_model, model_dir=model_dir, config=run_config)

  return estimator


def main(_):
  with logger.benchmark_context(FLAGS):
    run_ncf(FLAGS)


def run_ncf(_):
  """Run NCF training and eval loop."""
  if FLAGS.download_if_missing:
    movielens.download(FLAGS.dataset, FLAGS.data_dir)

  tf.logging.info("Data preprocessing...")
  ncf_dataset = pipeline.initialize(
      dataset=FLAGS.dataset, data_dir=FLAGS.data_dir, num_neg=FLAGS.num_neg)

  model_helpers.apply_clean(flags.FLAGS)

  # Create NeuMF model and convert it to Estimator
  tf.logging.info("Creating Estimator from Keras model...")
  layers = [int(layer) for layer in FLAGS.layers]
  mlp_regularization = [float(reg) for reg in FLAGS.mlp_regularization]
  keras_model = neumf_model.NeuMF(
      ncf_dataset.num_users, ncf_dataset.num_items, FLAGS.num_factors,
      layers, FLAGS.batch_size, FLAGS.mf_regularization,
      mlp_regularization)
  num_gpus = flags_core.get_num_gpus(FLAGS)
  estimator = convert_keras_to_estimator(keras_model, num_gpus, FLAGS.model_dir)

  # Create hooks that log information about the training and metric values
  train_hooks = hooks_helper.get_train_hooks(
      FLAGS.hooks,
      model_dir=FLAGS.model_dir,
      batch_size=FLAGS.batch_size  # for ExamplesPerSecondHook
  )
  run_params = {
      "batch_size": FLAGS.batch_size,
      "number_factors": FLAGS.num_factors,
      "hr_threshold": FLAGS.hr_threshold,
      "train_epochs": FLAGS.train_epochs,
  }
  benchmark_logger = logger.get_benchmark_logger()
  benchmark_logger.log_run_info(
      model_name="recommendation",
      dataset_name=FLAGS.dataset,
      run_params=run_params,
      test_id=FLAGS.benchmark_test_id)

  # Training and evaluation cycle
  def get_train_input_fn():
    return pipeline.get_input_fn(
        training=True,
        ncf_dataset=ncf_dataset,
        batch_size=distribution_utils.per_device_batch_size(
            FLAGS.batch_size, num_gpus),
        num_epochs=FLAGS.epochs_between_evals
    )

  def get_pred_input_fn():
    return pipeline.get_input_fn(
        training=False,
        ncf_dataset=ncf_dataset,
        batch_size=distribution_utils.per_device_batch_size(
            FLAGS.batch_size, num_gpus),
        num_epochs=1
    )

  total_training_cycle = FLAGS.train_epochs // FLAGS.epochs_between_evals

  for cycle_index in range(total_training_cycle):
    tf.logging.info("Starting a training cycle: {}/{}".format(
        cycle_index + 1, total_training_cycle))

    # Train the model
    estimator.train(input_fn=get_train_input_fn(), hooks=train_hooks)

    # Evaluate the model
    eval_results = evaluate_model(
        estimator, ncf_dataset, get_pred_input_fn())

    # Benchmark the evaluation results
    benchmark_logger.log_evaluation_result(eval_results)
    # Log the HR and NDCG results.
    hr = eval_results[_HR_KEY]
    ndcg = eval_results[_NDCG_KEY]
    tf.logging.info(
        "Iteration {}: HR = {:.4f}, NDCG = {:.4f}".format(
            cycle_index + 1, hr, ndcg))

    # If some evaluation threshold is met
    if model_helpers.past_stop_threshold(FLAGS.hr_threshold, hr):
      break

  # Clear the session explicitly to avoid session delete error
  tf.keras.backend.clear_session()


def define_ncf_flags():
  """Add flags for running ncf_main."""
  # Add common flags
  flags_core.define_base(export_dir=False)
  flags_core.define_performance(
      num_parallel_calls=False,
      inter_op=False,
      intra_op=False,
      synthetic_data=False,
      max_train_steps=False,
      dtype=False,
      all_reduce_alg=False
  )
  flags_core.define_benchmark()

  flags.adopt_module_key_flags(flags_core)

  flags_core.set_defaults(
      model_dir="/tmp/ncf/",
      data_dir="/tmp/movielens-data/",
      train_epochs=2,
      batch_size=256,
      hooks="ProfilerHook"
  )

  # Add ncf-specific flags
  flags.DEFINE_enum(
      name="dataset", default="ml-1m",
      enum_values=["ml-1m", "ml-20m"], case_sensitive=False,
      help=flags_core.help_wrap(
          "Dataset to be trained and evaluated."))

  flags.DEFINE_boolean(
      name="download_if_missing", default=True, help=flags_core.help_wrap(
          "Download data to data_dir if it is not already present."))

  flags.DEFINE_integer(
      name="num_factors", default=8,
      help=flags_core.help_wrap("The Embedding size of MF model."))

  # Set the default as a list of strings to be consistent with input arguments
  flags.DEFINE_list(
      name="layers", default=["64", "32", "16", "8"],
      help=flags_core.help_wrap(
          "The sizes of hidden layers for MLP. Example "
          "to specify different sizes of MLP layers: --layers=32,16,8,4"))

  flags.DEFINE_float(
      name="mf_regularization", default=0.,
      help=flags_core.help_wrap(
          "The regularization factor for MF embeddings. The factor is used by "
          "regularizer which allows to apply penalties on layer parameters or "
          "layer activity during optimization."))

  flags.DEFINE_list(
      name="mlp_regularization", default=["0.", "0.", "0.", "0."],
      help=flags_core.help_wrap(
          "The regularization factor for each MLP layer. See mf_regularization "
          "help for more info about regularization factor."))

  flags.DEFINE_integer(
      name="num_neg", default=4,
      help=flags_core.help_wrap(
          "The Number of negative instances to pair with a positive instance."))

  flags.DEFINE_float(
      name="learning_rate", default=0.001,
      help=flags_core.help_wrap("The learning rate."))

  flags.DEFINE_float(
      name="hr_threshold", default=None,
      help=flags_core.help_wrap(
          "If passed, training will stop when the evaluation metric HR is "
          "greater than or equal to hr_threshold. For dataset ml-1m, the "
          "desired hr_threshold is 0.68 which is the result from the paper; "
          "For dataset ml-20m, the threshold can be set as 0.95 which is "
          "achieved by MLPerf implementation."))


if __name__ == "__main__":
  tf.logging.set_verbosity(tf.logging.INFO)
  define_ncf_flags()
  FLAGS = flags.FLAGS
  absl_app.run(main)
