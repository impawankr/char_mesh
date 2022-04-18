# coding=utf-8
# Copyright 2022 The Mesh TensorFlow Authors.
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

r"""Utilities for running training and inference.

The `run` function for training the Transformer model is defined in this file.

TODO(katherinelee): add details about gin.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import functools
import itertools
import math
import os
import random
import re
import time

import gin
import gin.tf

import mesh_tensorflow as mtf
from mesh_tensorflow.transformer import dataset as transformer_dataset
from mesh_tensorflow.transformer import learning_rate_schedules
from mesh_tensorflow.transformer import transformer
import numpy as np
import pkg_resources
import six
import tensorflow.compat.v1 as tf
from tensorflow.compat.v1 import estimator as tf_estimator
import tensorflow_datasets as tfds

from tensorflow.core.protobuf import rewriter_config_pb2  # pylint: disable=g-direct-tensorflow-import
from tensorflow.python.ops import resources  # pylint: disable=g-direct-tensorflow-import
from tensorflow.python.tpu import tpu_config  # pylint: disable=g-direct-tensorflow-import
from tensorflow.python.tpu import tpu_estimator  # pylint: disable=g-direct-tensorflow-import

try:
  tf.flags.DEFINE_multi_string("gin_file", None, "Path to a Gin file.")
  tf.flags.DEFINE_multi_string("gin_param", None, "Gin parameter binding.")
  tf.flags.DEFINE_list("gin_location_prefix", [], "Gin file search path.")
except tf.flags.DuplicateFlagError:
  pass

FLAGS = tf.flags.FLAGS

_DEFAULT_CONFIG_FILE = "./gin/defaults.gin"

# List of features used by model.
_MODEL_FEATURES = [
    "inputs", "inputs_position", "inputs_segmentation", "targets",
    "targets_position", "targets_segmentation", "targets_subsegmentation"
]


def _filter_features(ex):
  """Filters example features, keeping only valid model features."""
  return {k: v for k, v in ex.items() if k in _MODEL_FEATURES}


def parse_gin_defaults_and_flags(skip_unknown=False, finalize_config=True):
  """Parses all default gin files and those provided via flags."""
  # Register .gin file search paths with gin
  for gin_file_path in FLAGS.gin_location_prefix:
    gin.add_config_file_search_path(gin_file_path)
  # Set up the default values for the configurable parameters. These values will
  # be overridden by any user provided gin files/parameters.
  gin.parse_config_file(
      pkg_resources.resource_filename(__name__, _DEFAULT_CONFIG_FILE),
      skip_unknown=skip_unknown)
  gin.parse_config_files_and_bindings(
      FLAGS.gin_file, FLAGS.gin_param,
      skip_unknown=skip_unknown,
      finalize_config=finalize_config)


# TODO(noam): maybe add gin-config to mtf.get_variable so we can delete
#  this stupid VariableDtype class and stop passing it all over creation.
@gin.configurable
def get_variable_dtype(
    master_dtype=tf.bfloat16,
    slice_dtype=tf.float32,
    activation_dtype=tf.float32):
  """Datatypes to use for the run.

  Args:
    master_dtype: string, datatype for checkpoints
      keep this the same between training and eval/inference
    slice_dtype: string, datatype for variables in memory
      must be tf.float32 for training
    activation_dtype: string, datatype for activations
      less memory usage if tf.bfloat16 but possible numerical issues
  Returns:
    a mtf.VariableDtype
  """
  return mtf.VariableDType(
      master_dtype=tf.as_dtype(master_dtype),
      slice_dtype=tf.as_dtype(slice_dtype),
      activation_dtype=tf.as_dtype(activation_dtype))


def inputs_vocabulary(vocabulary):
  """Get the inputs vocabulary.

  Args:
    vocabulary: Vocabulary or (inputs_vocabulary, targets_vocabulary) tuple.

  Returns:
    a Vocabulary
  """
  if isinstance(vocabulary, tuple):
    vocabulary = vocabulary[0]
  return vocabulary


def targets_vocabulary(vocabulary):
  """Get the targets vocabulary.

  Args:
    vocabulary: Vocabulary or (inputs_vocabulary, targets_vocabulary) tuple.

  Returns:
    a Vocabulary
  """
  if isinstance(vocabulary, tuple):
    vocabulary = vocabulary[1]
  return vocabulary


@gin.configurable
def separate_vocabularies(inputs=gin.REQUIRED, targets=gin.REQUIRED):
  """Gin-configurable helper function to generate a tuple of vocabularies."""
  return (inputs, targets)


@gin.configurable
def init_checkpoint_variable_mapping(name, mapping_fn=None):
  """Maps from variable name in graph to variable name in checkpoint."""
  if mapping_fn:
    return mapping_fn(name)
  else:
    return name


@gin.configurable
def should_load_variable(name, filter_fn=None):
  """Determines whether a global variable should be loaded from a ckpt."""
  if filter_fn:
    return filter_fn(name)
  else:
    return True


# TODO(katherinelee): Update layout_rules string when noam updates the
# definition in run
def build_model(model_type="bitransformer",
                input_vocab_size=gin.REQUIRED,
                output_vocab_size=gin.REQUIRED,
                layout_rules=None,
                mesh_shape=None):
  """Build a transformer model.

  Currently, four types of models are supported:

  "bitransformer": The traditional encoder-decoder architecture from
     "Attention is All You Need".  Requires a non-text2self dataset.

  "lm": an autoregressive language model (one layer stack).  Effectively the
     decoder of the bitransformer. There is no attention over the encoder, since
     there is no encoder.  Requires a text2self dataset, with targets, but no
     inputs.

  "delimited_lm": an autoregressive language model trained on a text2text
     dataset.  Each training example is expressed as
     [<input_tokens>, EOS, <target_tokens>, EOS].  Model checkpoints are
     compatible with "lm" models.  One strategy is to pretrain as "lm"
     then fine-tune as "delimited_lm".

  "aligned": a non-autoregressive single-stack model (like BERT).  Requires
     a non-text2self dataset with inputs and targets.  The targets and inputs
     have the same length and each entry in the inputs is aligned to the
     corresponding entry in targets, eg:
      "inputs": "The X sat on X X."
      'targets": "The cat sat on the mat."
      (except, inputs are token ID sequences, not strings)

  "bi_teacher_student": a teacher-student model where both the student and
    teacher are bitransformers. Requires a non-text2self dataset.

  A text2self dataset has targets that are offset of the inputs. Non-text2self
  datasets have targets that differ from their inputs, like:
    input: 'hello'
    target: 'bonjour'

  Args:
    model_type: a string, one of "bitransformer", "lm", "delimited_lm",
      "aligned", or "bi_teacher_student"
    input_vocab_size: an integer
    output_vocab_size: an integer
    layout_rules: optional, input to mtf.convert_to_layout_rules
    mesh_shape: optional, an input to mtf.convert_to_shape()
  Returns:
    a Unitransformer or Bitransformer
  """
  if model_type == "bitransformer":
    return transformer.make_bitransformer(
        input_vocab_size=input_vocab_size,
        output_vocab_size=output_vocab_size,
        mesh_shape=mesh_shape,
        layout=layout_rules)
  elif model_type == "bi_student_teacher":
    return transformer.make_bi_student_teacher(
        input_vocab_size=input_vocab_size,
        output_vocab_size=output_vocab_size,
        mesh_shape=mesh_shape,
        layout=layout_rules)
  elif model_type in ["lm", "delimited_lm", "aligned"]:
    return transformer.Unitransformer(
        autoregressive=model_type in ["lm", "delimited_lm"],
        layer_stack=transformer.make_layer_stack(),
        input_vocab_size=input_vocab_size,
        output_vocab_size=output_vocab_size,
        mesh_shape=mesh_shape,
        layout=layout_rules)
  else:
    raise ValueError("unknown model_type")


@gin.configurable
def tpu_mesh_shape(tpu_topology=gin.REQUIRED,
                   model_parallelism=gin.REQUIRED,
                   ensemble_parallelism=None):
  """Create a mesh_shape for data-parallelism and model-parallelism on TPU.

  Example: tpu_mesh_shape("4x4", 8) -> mtf.Shape(("batch", 4), ("model", 8))
  Since there are 4x4x2=32 total cores, and we want 8-way model paralleism.

  This function is passed through gin to the argument `mesh_shape` inside the
  function `run`.

  Alternatively, for model_parallelism, pass a mesh_spec (see simd_mesh_impl.py)
  TODO(noam): describe

  Args:
    tpu_topology: a string - e.g. "2x2" or "v3-8"
    model_parallelism: an integer - the number of cores per model replica
      alternatively a list that can be passed to
      simd_mesh_impl.HierarchicalTiling
    ensemble_parallelism: an optional integer - if present then create an
      "ensemble" mesh-dimension as well, for splitting the models in an
      ensemble.
  Returns:
    a mtf.Shape
  """
  if tpu_topology.startswith("v"):
    num_cores = int(tpu_topology.split("-")[-1])
  else:
    # check for twisted topologies
    tpu_topology = re.split("_twisted|_untwisted", tpu_topology)[0]
    tpu_dim = [int(x) for x in tpu_topology.split("x")]
    num_cores = functools.reduce(lambda x, y: x * y,
                                 tpu_dim) * FLAGS.logical_cores_per_chip
  if isinstance(model_parallelism, list):
    # model_parallelism is actually a spec used to
    # construct a simd_mesh_impl.HierarchicalTiling object
    return mtf.simd_mesh_impl.HierarchicalTiling.spec_to_mesh_shape(
        model_parallelism, num_cores)
  data_parallelism = num_cores // model_parallelism
  if ensemble_parallelism:
    data_parallelism //= ensemble_parallelism
  dims = []
  if ensemble_parallelism and ensemble_parallelism > 1:
    dims.append(mtf.Dimension("ensemble", ensemble_parallelism))
  if data_parallelism > 1:
    dims.append(mtf.Dimension("batch", data_parallelism))
  if model_parallelism > 1:
    dims.append(mtf.Dimension("model", model_parallelism))
  return mtf.Shape(dims)


@gin.configurable
def variable_filter_max_size(v, max_size=1e7):
  return v.size <= max_size


def _build_ckpt_to_local_var_name_mapping(
    ckpt_num_blocks, ckpt_num_layers, local_num_blocks,
    local_num_layers, new_layers, regex_prefix=None):
  """Builds a mapping from checkpoint variable names to local variable names.

  Args:
    ckpt_num_blocks: an integer, number of blocks in checkpoint.
    ckpt_num_layers: an integer, number of layers in checkpoint.
    local_num_blocks: an integer, number of blocks in current model.
    local_num_layers: an integer, number of layers in current model.
    new_layers: a list of lists, specifying new layer indices in the current
      model not present in the ckpt.
    regex_prefix: optional, a string, specifying a prefix to match for
      both checkpoint variables and ones in the current model.

  Returns:
    a dictionary where keys are checkpoint variable name regexes and
    values are local variable name regexes. It specifies the mapping between
    the checkpoint block/layer group and local block/layer group.
  """
  def build_regex(layer_num, block_num, num_blocks):
    base_regex = r"layer_{:0=3d}".format(layer_num)
    if num_blocks is not None:
      base_regex = r"block_{:0=3d}/".format(block_num) + base_regex
    if regex_prefix is not None:
      base_regex = regex_prefix + r".*" + base_regex
    return base_regex

  all_ckpt_name_regexes = []
  for block_num in range(ckpt_num_blocks or 1):
    for layer_num in range(ckpt_num_layers):
      all_ckpt_name_regexes.append(
          build_regex(layer_num, block_num, ckpt_num_blocks))

  all_local_name_regexes = []
  for block_num in range(local_num_blocks or 1):
    for layer_num in range(local_num_layers):
      # Skip the new layers in the mapping ordering.
      if (new_layers is not None) and (layer_num in new_layers): continue
      all_local_name_regexes.append(
          build_regex(layer_num, block_num, local_num_blocks))

  if len(all_ckpt_name_regexes) != len(all_local_name_regexes):
    raise ValueError("Invalid checkpoint to load. Number of variables in ckpt "
                     "and current model (minus `new_layers`) must be equal.")

  # Build a mapping from ckpt var regex to local var regex.
  ckpt_var_name_to_local_var_name = {}
  for ckpt_var_name, local_var_name in zip(
      all_ckpt_name_regexes, all_local_name_regexes):
    ckpt_var_name_to_local_var_name[ckpt_var_name] = local_var_name
  return ckpt_var_name_to_local_var_name


def _match_ckpt_to_local_var_name(
    ckpt_var_name, local_var_name, ckpt_var_name_to_local_var_name):
  """Returns True if this pair of vars should be loaded, False otherwise."""
  # Name does not fall into the block/layer convention, so return identity.
  # This will cover variable such as the global_step, embeddings, etc...
  if "layer" not in ckpt_var_name or "layer" not in local_var_name:
    return ckpt_var_name == local_var_name

  # If the variable suffixes do not match they cannot be matched.
  if ckpt_var_name.split("/")[-1] != local_var_name.split("/")[-1]:
    return False

  for ckpt_regex, var_regex in ckpt_var_name_to_local_var_name.items():
    if (re.match(ckpt_regex, ckpt_var_name) and
        re.match(var_regex, local_var_name)):
      # Both ckpt and local var are the same layer/block group. Now check to
      # see if its the same parameter in the layer/block group.
      if ".*" in ckpt_regex:
        ckpt_regex = ckpt_regex.split(".*")[1]
      if ".*" in var_regex:
        var_regex = var_regex.split(".*")[1]
      if ckpt_var_name.replace(ckpt_regex, var_regex) == local_var_name:
        return True
  return False


def _compute_num_blocks_and_layer(var_names):
  """Takes list of variable names and outputs the max number of blocks/layers."""

  encoder_decoder_model = any(
      [re.match(r"^encoder/", var_name) for var_name in var_names])

  def get_max_layer_or_block_num(regex, var_names):
    matched_nums = [re.findall(regex, v) for v in var_names]
    return max(
        [int(num) + 1 for num in list(itertools.chain(*matched_nums))] + [-1])

  if encoder_decoder_model:
    enc_max_layer_num = get_max_layer_or_block_num(
        r"encoder/.*layer_(\d{3})", var_names)
    enc_max_block_num = get_max_layer_or_block_num(
        r"encoder/.*block_(\d{3})", var_names)
    dec_max_layer_num = get_max_layer_or_block_num(
        r"decoder/.*layer_(\d{3})", var_names)
    dec_max_block_num = get_max_layer_or_block_num(
        r"decoder/.*block_(\d{3})", var_names)
    max_layer_num = [enc_max_layer_num, dec_max_layer_num]
    max_block_num = [enc_max_block_num, dec_max_block_num]
  else:
    max_layer_num = [get_max_layer_or_block_num(r"layer_(\d{3})", var_names)]
    max_block_num = [get_max_layer_or_block_num(r"block_(\d{3})", var_names)]

  max_block_num = [n if (n != -1) else None for n in max_block_num]
  return max_block_num, max_layer_num


@gin.configurable
def flexible_ckpt_init_mapping(ckpt_path="", new_layers=None,
                               return_mapping_fn=True):
  """More flexibly handles loading a checkpoint.

  To be used as a mapping_fn in init_checkpoint_variable_mapping or as a
  filter_fn in should_load_variable depending on return_mapping_fn.

  Covers three common cases when initializing a checkpoint:
  (1) Loading a checkpoint that contains different block/layer numbering.
  (2) Inserting new layers in into the current model that should not be loaded
      from the checkpoint (e.g. inserting an extra DenseReLUDense layer in each
      block group for the current model).
  (3) Changing the layer type from the ckpt in the current model
      (e.g. replacing a DenseReLUDense layer with an MoE1D layer).

  Args:
    ckpt_path: string, saved checkpoint path to load.
    new_layers: optional list of lists specifing what numbers in the layer stack
      are newly added in the current model. These should be skipped when loading
      the checkpoint weights. If Enc-Dec model the list will contains two lists,
      one for new encoder layers and the other for decoder layer
      (e.g. [[3], [1]]). If LM then just a single list (e.g. [[3]]).
    return_mapping_fn: a boolean, if True then return a function mapping from
      the graph variable names to the checkpoint variable names that should be
      loaded in. If False, then return a filter_fn that will return whether
      a graph variable should be loaded from the ckpt.

  Returns:
    if return_mapping_fn is True then return a function mapping from
    the graph variable names to the checkpoint variable names.
    If False, then return a filter_fn that will return whether a graph
    variable should be loaded from the ckpt.
  """
  tf.logging.info("Using flexible_ckpt_init_mapping.")
  ckpt_var_names = [v for v, _ in tf.train.list_variables(ckpt_path)]
  local_var_names = [v.op.name for v in tf.global_variables()]

  # `num_blocks` and `num_layers` will be tuples of length two for
  # encoder-decoder models and length 1 for LMs.
  ckpt_num_blocks, ckpt_num_layers = _compute_num_blocks_and_layer(
      ckpt_var_names)
  local_num_blocks, local_num_layers = _compute_num_blocks_and_layer(
      local_var_names)

  # Create regex mapping from ckpt variable names to local variable names.
  mappings = []
  if len(ckpt_num_blocks) == 2:
    # Encoder-Decoder Model.
    new_enc_layers, new_dec_layers = None, None
    if new_layers is not None:
      new_enc_layers, new_dec_layers = new_layers
    enc_mapping = _build_ckpt_to_local_var_name_mapping(
        ckpt_num_blocks[0], ckpt_num_layers[0], local_num_blocks[0],
        local_num_layers[0], new_enc_layers, regex_prefix="encoder/")
    dec_mapping = _build_ckpt_to_local_var_name_mapping(
        ckpt_num_blocks[1], ckpt_num_layers[1], local_num_blocks[1],
        local_num_layers[1], new_dec_layers, regex_prefix="decoder/")
    mappings = [enc_mapping, dec_mapping]
  else:
    # LM Model.
    new_lm_layers = None
    if new_layers is not None:
      new_lm_layers = new_layers[0]
    lm_mapping = _build_ckpt_to_local_var_name_mapping(
        ckpt_num_blocks[0], ckpt_num_layers[0], local_num_blocks[0],
        local_num_layers[0], new_lm_layers)
    mappings = [lm_mapping]

  graph_var_to_ckpt_var = {}
  for ckpt_var_name in ckpt_var_names:
    for local_var_name in local_var_names:
      for mapping in mappings:
        if _match_ckpt_to_local_var_name(
            ckpt_var_name, local_var_name, mapping):
          graph_var_to_ckpt_var[local_var_name] = ckpt_var_name

  def mapping_fn(var_name):
    return graph_var_to_ckpt_var[var_name]

  def filter_fn(var_name):
    return var_name in graph_var_to_ckpt_var

  if return_mapping_fn:
    return mapping_fn
  else:
    return filter_fn


@gin.configurable(denylist=["predict_fn"])  # pass `predict_fn` through `run`
def tpu_estimator_model_fn(model_type,
                           transformer_model,
                           vocabulary,
                           model_dir,
                           use_tpu,
                           mesh_shape,
                           layout_rules,
                           batch_size,
                           sequence_length,
                           autostack,
                           keep_checkpoint_max,
                           save_checkpoints_steps,
                           learning_rate_schedule=None,
                           optimizer=None,
                           outer_batch_size=1,
                           tpu_summaries=False,
                           predict_fn=None,
                           score_in_predict_mode=False,
                           variable_filter=None,
                           init_checkpoint=None,
                           init_variable_filter="",
                           ensemble_inputs=None,
                           mesh_devices=None,
                           model_info_file=None,
                           hierarchical_tiling_spec=None,
                           weight_decay_checkpoint=None  # GOOGLE-INTERNAL,
                           ):
  """Create a TPUEstimator model function.

  Args:
    model_type: a string. One of "bitransformer", "lm", "delimited_lm",
      "aligned", or "bi_teacher_student"
    transformer_model: a transformer.Unitransformer or transformer.Bitransformer
    vocabulary: a vocabulary.Vocabulary or (inputs_vocabulary,
      targets_vocabulary) tuple. Used for decoding in predict mode.
    model_dir: a string, directory to save the model to.
    use_tpu: a boolean
    mesh_shape: a mtf.Shape
    layout_rules: a mtf.LayoutRules
    batch_size: an integer
    sequence_length: an integer or a dict from feature-key to integer
      the (packed) sequence length, e.g. {"inputs": 512, "targets": 128}
    autostack: a boolean
    keep_checkpoint_max: an integer, maximum number of checkpoints to keep
    save_checkpoints_steps: an integer, save a checkpoint every this number of
      steps
    learning_rate_schedule: a constant or a function from step to learning rate
    optimizer: a class extending optimize.Optimizer, required for training
    outer_batch_size: outer batch dimension that could be used to enable the mix
      of data-parallel and model-parallel training of Mixture of Experts (MoE)
      models
    tpu_summaries: a boolean, use rewrites to make summaries work on TPU.  This
      may be slow, since it uses a host call hack.
    predict_fn: an optional function, see docs for `run` for more information.
    score_in_predict_mode: compute log-likelihood scores instead of predictions
    variable_filter: controls which variables are trained.
      If None (default), train all trainable variables.
      If a string regex, train all variables that match this regex.
      If a function (mtf.Variable -> boolean), then train variables for which
        the function returns True.
    init_checkpoint: a string, if not None then read in variables from this
      checkpoint path when initializing variables. Will only initialize
      variables that appear both in the current graph and the checkpoint.
    init_variable_filter: a string, used only when init_checkpoint is set.
      controls which variables are loaded from the checkpoint using regex.
      if empty string (default), all variables from the checkpoint are loaded.
    ensemble_inputs: an optional integer - pass the size of the ensemble to
      train an ensemble where each model gets different inputs.
      You also need to configure Unitransformer.ensemble to the right size.
      If None, then all models are trained on the same inputs.
    mesh_devices: a list of strings, the device names to use for each mesh
      slice. Only required for GPU.
    model_info_file: an optional string, information about variables and
      operations will be logged to this file during the TRAIN mode.
    hierarchical_tiling_spec: an optional list that can be passed as the
      spec argument to simd_mesh_impl.HierarchicalTiling
    weight_decay_checkpoint: an optional checkpoint dir to weight decay from.  #
      GOOGE-INTERNAL

  Returns:
    a function to be passed to TPUEstimator
  """
  mesh_devices = mesh_devices or [""] * mesh_shape.size

  def my_model_fn(features, labels, mode, params=None, config=None):
    """Estimator model function.

    Args:
      features: dictionary where keys are strings like "inputs" and "targets"
        and the values are the actual values of "inputs". See TPUEstimator's
        docs for more information
      labels: ignored argument
      mode: a tf.estimator.ModeKeys
      params: dictionary containing the key "context"
      config: ignored argument

    Returns:
      a TPUEstimatorSpec
    """
    del labels, config
    if mode == tf_estimator.ModeKeys.PREDICT and score_in_predict_mode:
      mode = "score"
    global_step = tf.train.get_global_step()
    if use_tpu and "context" in params:
      ctx = params["context"]
      num_hosts = ctx.num_hosts
      host_placement_fn = ctx.tpu_host_placement_function
      device_list = [host_placement_fn(host_id=t) for t in range(num_hosts)]
      # TODO(ylc): Better estimation of replica cache size?
      replica_cache_size = 300 * 1000000  # 300M per replica
      # Worker 0 caches all the TPU binaries.
      worker0_mem = replica_cache_size * ctx.num_replicas
      devices_memeory_usage = [worker0_mem] + [0] * (num_hosts - 1)
      var_placer = mtf.utils.BalancedVariablePlacer(device_list,
                                                    devices_memeory_usage)
      physical_shape = [int(i) for i in
                        params["context"].device_assignment.topology.mesh_shape]
      mesh_4d = False
      if len(physical_shape) == 4:
        mesh_4d = True if physical_shape[2] > 1 else False
        physical_shape = (
            mtf.simd_mesh_impl.physical_shape_3d_from_topology_proto_4d(
                physical_shape))
      if mesh_4d or hierarchical_tiling_spec is None:
        logical_to_physical = mtf.simd_mesh_impl.auto_logical_to_physical_tpu(
            mesh_shape.to_integer_list,
            physical_shape,
            device_assignment=params["context"].device_assignment)
      else:
        logical_to_physical = mtf.simd_mesh_impl.HierarchicalTiling(
            hierarchical_tiling_spec,
            physical_shape).logical_to_physical
      mesh_impl = mtf.simd_mesh_impl.SimdMeshImpl(
          mesh_shape, layout_rules, mesh_devices, ctx.device_assignment,
          logical_to_physical=logical_to_physical)
    else:
      var_placer = None
      mesh_impl = mtf.placement_mesh_impl.PlacementMeshImpl(
          mesh_shape, layout_rules, mesh_devices)

    graph = mtf.Graph()
    mesh = mtf.Mesh(graph, "my_mesh", var_placer)

    if (outer_batch_size and
        mode not in [tf_estimator.ModeKeys.PREDICT, "score"]):
      outer_batch_dim = mtf.Dimension("outer_batch", outer_batch_size)
      batch_dim = mtf.Dimension("batch", batch_size // outer_batch_size)
      batch_dims = [outer_batch_dim, batch_dim]
    else:
      batch_dim = mtf.Dimension("batch", batch_size)
      batch_dims = [batch_dim]
    ensemble_dims = ([mtf.Dimension("ensemble", ensemble_inputs)]
                     if ensemble_inputs else [])

    predict_batch_size = features.pop("predict_batch_size", None)

    mtf_features = {}
    for key, x in features.items():
      # Some auxiliary features may have been generated in packing.
      # The names of these new features are of the form
      #   "<original_feature_name>_<suffix>", e.g. "inputs_segmentation".
      #   We look up the lengths based on the original feature name, without
      #   the "_<suffix>".
      feature_length = sequence_length[key.split("_")[0]]
      length_dim = mtf.Dimension("length", feature_length)
      feature_shape = mtf.Shape(
          ensemble_dims + batch_dims + [length_dim])
      x = tf.cast(features[key], tf.int32)
      x = tf.reshape(x, feature_shape.to_integer_list)
      if not use_tpu:
        tf.logging.info("feature %s : %s", key, x)
      mtf_features[key] = mtf.import_fully_replicated(
          mesh, x, feature_shape, name=key)

    def _verify_feature_exists(feature_name, should_exist):
      if should_exist != (feature_name in mtf_features):
        message = (
            "mode=%s model_type=%s should%s have feature %s" %
            (mode, model_type, "" if should_exist else " not", feature_name))
        if "lm" in model_type:
          message += (
              "\nA common mistake is that model_type=\"delimited_lm\" should "
              "be used with tasks that produce inputs and targets, while "
              "model_type=\"lm\" should be used with tasks that produce "
              "targets only.")
        raise ValueError(message)

    # Verify that the right features exist, and transform them if necessary
    if mode == tf_estimator.ModeKeys.PREDICT:
      _verify_feature_exists("inputs", True)
      # "targets" may or may not exist depending on whether we are doing
      # evaluation or open-ended inference.
    elif model_type in ("lm", "delimited_lm") and mode == "score":
      # in scoring mode the inputs and targets may already be combined.
      if "inputs" in mtf_features:
        if model_type == "lm":
          tf.logging.warning(
              "Scoring of lm models will include loss from the 'inputs'.")
        mtf_features = _dynamic_text2self(mtf_features)
    else:
      _verify_feature_exists("targets", True)
      _verify_feature_exists("inputs", model_type != "lm")
      if model_type == "delimited_lm":
        mtf_features = _dynamic_text2self(mtf_features)

    # Detokenize in the graph if supported by vocabulary and accelerator.
    def _maybe_detokenize(ids, vocab):
      if not use_tpu and hasattr(vocab, "decode_tf"):
        return vocab.decode_tf(ids)
      return ids
    if mode == "score":
      # compute log-likelihoods per sequence
      targets = mtf_features["targets"]
      if predict_fn:
        # predict_fn contains a custom scoring function
        scores = predict_fn(
            model=transformer_model,
            features=mtf_features,
            variable_dtype=get_variable_dtype())
      else:
        if isinstance(transformer_model, transformer.Unitransformer):
          length_dim = targets.shape.dims[-1]
          inputs = transformer.autoregressive_inputs(
              mtf_features["targets"])
        elif isinstance(transformer_model,
                        (transformer.Bitransformer,
                         transformer.StudentTeacher)):
          inputs = mtf_features["inputs"]
        else:
          raise ValueError("unrecognized class")
        logits, _ = transformer_model.call_simple(
            inputs=inputs,
            targets=targets,
            compute_loss=False,
            mode=mode,
            variable_dtype=get_variable_dtype())
        logits = mtf.cast(logits, tf.float32)
        _, length_dim, vocab_dim = logits.shape.dims

        cross_entropy = mtf.layers.softmax_cross_entropy_with_logits(
            logits, mtf_features["targets"], vocab_dim)
        # 0=padding and negative targets are a hack to indicate no loss
        cross_entropy *= mtf.cast(
            mtf.greater(targets, 0), cross_entropy.dtype)
        if model_type == "delimited_lm":
          cross_entropy *= mtf.cast(mtf.logical_not(
              transformer.delimited_lm_inputs_mask(targets)),
                                    cross_entropy.dtype)
        scores = -mtf.reduce_sum(cross_entropy, reduced_dim=length_dim)

      scores = mtf.anonymize(scores)
      targets = mtf.anonymize(targets)
      lowering = mtf.Lowering(graph, {mesh: mesh_impl}, autostack=autostack)
      targets = clean_decodes(lowering.export_to_tf_tensor(targets))
      targets = _maybe_detokenize(targets, targets_vocabulary(vocabulary))

      predictions = {
          "targets": targets,
          "scores": lowering.export_to_tf_tensor(scores)
      }
    elif mode == tf_estimator.ModeKeys.PREDICT:
      inputs = mtf_features["inputs"]
      if predict_fn:
        mtf_samples = predict_fn(
            model=transformer_model,
            features=mtf_features,
            variable_dtype=get_variable_dtype())
      elif isinstance(transformer_model, transformer.Unitransformer):
        # pad so that there is enough room for the targets
        inputs = mtf.pad(
            inputs, [0, sequence_length["targets"]], length_dim.name)
        mtf_samples = transformer_model.sample_autoregressive(
            inputs, variable_dtype=get_variable_dtype(),
            remove_partial_sequences=True)
      elif isinstance(
          transformer_model,
          (transformer.Bitransformer, transformer.StudentTeacher)):
        mtf_samples = transformer_model.decode(
            inputs, variable_dtype=get_variable_dtype())
      else:
        raise ValueError("unrecognized class")
      mtf_samples = mtf.anonymize(mtf_samples)
      inputs = mtf.anonymize(inputs)
      lowering = mtf.Lowering(graph, {mesh: mesh_impl}, autostack=autostack)
      inputs = clean_decodes(lowering.export_to_tf_tensor(inputs))
      outputs = clean_decodes(lowering.export_to_tf_tensor(mtf_samples))

      inputs = _maybe_detokenize(inputs, inputs_vocabulary(vocabulary))
      outputs = _maybe_detokenize(outputs, targets_vocabulary(vocabulary))

      if predict_batch_size is not None:
        inputs = inputs[:predict_batch_size]
        outputs = outputs[:predict_batch_size]

      predictions = {
          "inputs": inputs,
          "outputs": outputs}

    if mode in ["score", tf_estimator.ModeKeys.PREDICT]:
      # When exporting a model, we need to communicate to TF-Serving that
      # master variables need to be copied to their slave slice variables.
      # Estimator uses a Scaffold's "local_init_op" for this purpose, so we
      # augment the default "local_init_op" here.
      #
      # The "ready_op" is also constructed here to ensure the variables
      # initialized by "local_init_op" are the same ones checked by "ready_op".
      #
      # WARNING: Any variables created outside of this model_fn()
      # (e.g. tpu_estimator/iterations_per_loop) will NOT be initialized nor
      # checked by these ops.
      def scaffold_fn():
        return tf.train.Scaffold(
            local_init_op=tf.group(
                tf.train.Scaffold.default_local_init_op(),
                lowering.copy_masters_to_slices(),
                name="mtf_local_init_op"),
            ready_op=tf.concat(
                [tf.report_uninitialized_variables(),
                 resources.report_uninitialized_resources()],
                axis=0,
                name="mtf_ready_op"))

      return tpu_estimator.TPUEstimatorSpec(
          mode=tf_estimator.ModeKeys.PREDICT,
          predictions=predictions,
          scaffold_fn=scaffold_fn,
          prediction_hooks=[mtf.MtfRestoreHook(lowering)])

    assert (mode == tf_estimator.ModeKeys.TRAIN or
            mode == tf_estimator.ModeKeys.EVAL)

    def logits_and_loss(mtf_features, num_microbatches=1):
      """Compute logits and loss.

      Args:
        mtf_features: a dictionary
        num_microbatches: integer
      Returns:
        logits: a mtf.Tensor
        loss: a mtf.Tensor
      """
      if model_type in ["lm", "delimited_lm"]:
        inputs = transformer.autoregressive_inputs(
            mtf_features["targets"],
            sequence_id=mtf_features.get("targets_segmentation", None))
      else:
        inputs = mtf_features["inputs"]

      if isinstance(transformer_model, transformer.Unitransformer):
        position_kwargs = dict(
            sequence_id=mtf_features.get("targets_segmentation", None),
            position=mtf_features.get("targets_position", None),
        )
      elif isinstance(
          transformer_model,
          transformer.Bitransformer) or model_type == "bi_student_teacher":
        position_kwargs = dict(
            encoder_sequence_id=mtf_features.get("inputs_segmentation", None),
            decoder_sequence_id=mtf_features.get("targets_segmentation",
                                                 None),
            decoder_subsequence_id=mtf_features.get("targets_subsegmentation",
                                                    None),
            encoder_position=mtf_features.get("inputs_position", None),
            decoder_position=mtf_features.get("targets_position", None),
        )
      else:
        raise ValueError("unrecognized class")

      return transformer_model.call_simple(
          inputs=inputs,
          targets=mtf_features["targets"],
          compute_loss=True,
          mode=mode,
          variable_dtype=get_variable_dtype(),
          num_microbatches=num_microbatches,
          **position_kwargs)

    if mode == tf_estimator.ModeKeys.TRAIN:
      num_microbatches = serialize_num_microbatches(batch_dim,
                                                    sequence_length,
                                                    mesh_shape,
                                                    layout_rules)
      if num_microbatches > 1:
        def serialized_fn(mtf_features):
          return {"loss": logits_and_loss(mtf_features, num_microbatches)[1]}
        var_grads, loss_dict = mtf.serialize_training_step(
            mtf_features, serialized_fn, batch_dim, num_microbatches)
        loss = loss_dict["loss"]
      else:
        loss = logits_and_loss(mtf_features)[1]
        var_grads = mtf.gradients(
            [loss], [v.outputs[0] for v in graph.trainable_variables])


      if tpu_summaries:
        mtf.scalar_summary("loss", loss)

      if callable(learning_rate_schedule):
        # the following happens on CPU since TPU can't handle summaries.
        with mtf.utils.outside_all_rewrites():
          learning_rate = learning_rate_schedule(
              step=tf.train.get_global_step())
          tf.summary.scalar("learning_rate", learning_rate)
      else:
        learning_rate = learning_rate_schedule

      if isinstance(variable_filter, str):
        pattern = re.compile(variable_filter)
        variable_filter_fn = lambda v: pattern.search(v.name)
      elif variable_filter is None:
        variable_filter_fn = lambda v: True
      elif callable(variable_filter):
        variable_filter_fn = variable_filter
      else:
        raise ValueError(
            "variable_filter must be None, a string, or a callable function")
      trainable_vars = [
          v for v in graph.trainable_variables if variable_filter_fn(v)]
      trainable_var_grads = [
          g for g, v in zip(var_grads, graph.trainable_variables)
          if variable_filter_fn(v)]
      if len(trainable_vars) != len(graph.trainable_variables):
        tf.logging.info("Variables being trained:")
        tf.logging.info([v.name for v in trainable_vars])
        tf.logging.info("Variables not being trained:")
        tf.logging.info([v.name for v in graph.trainable_variables
                         if not variable_filter_fn(v)])
      opt = optimizer(learning_rate=learning_rate)
      update_ops = opt.apply_grads(trainable_var_grads, trainable_vars)
      lowering = mtf.Lowering(
          graph, {mesh: mesh_impl},
          autostack=autostack,
          log_file=model_info_file)

      tf_loss = lowering.export_to_tf_tensor(loss)
      tf_loss = tf.cast(tf_loss, tf.float32)
      if not use_tpu:
        tf_loss = tf.Print(tf_loss, [tf_loss, tf.train.get_global_step()],
                           "step, tf_loss")

      tf_update_ops = [lowering.lowered_operation(op) for op in update_ops]
      tf_update_ops.append(tf.assign_add(global_step, 1))
      train_op = tf.group(tf_update_ops)

      if hasattr(transformer_model, "initialize"):
        with mtf.utils.outside_all_rewrites():
          transformer_model.initialize()

      if tpu_summaries:
        # has to be outside of
        # with mtf.utils.outside_all_rewrites()
        host_call = mtf.utils.create_host_call(model_dir)
        mtf.utils.remove_summaries()
      else:
        host_call = None

      with mtf.utils.outside_all_rewrites():

        if init_checkpoint:
          ckpt_vars = {v for v, _ in tf.train.list_variables(init_checkpoint)}

          if init_variable_filter:
            pattern = re.compile(init_variable_filter)
            ckpt_vars = {v for v in ckpt_vars if pattern.search(v)}

          global_vars = {v.op.name for v in tf.global_variables()}
          filtered_global_vars = {
              v for v in global_vars if should_load_variable(v)}
          restore_vars = {
              v for v in filtered_global_vars
              if init_checkpoint_variable_mapping(v) in ckpt_vars}
          tf.logging.info("Initializing variables from %s:", init_checkpoint)
          tf.logging.debug("\n".join(sorted(restore_vars)))
          tf.logging.info("Variables in %s but not in graph:", init_checkpoint)
          tf.logging.info("\n".join(sorted(
              ckpt_vars -
              {init_checkpoint_variable_mapping(v)
               for v in filtered_global_vars})))
          tf.logging.info("Variables in graph but not in %s:", init_checkpoint)
          tf.logging.info("\n".join(sorted(global_vars - restore_vars)))
          tf.train.init_from_checkpoint(
              init_checkpoint,
              {init_checkpoint_variable_mapping(v): v for v in restore_vars}
          )


        # Copy master variables to slices. Must be called first.
        restore_hook = mtf.MtfRestoreHook(lowering)
        saver = tf.train.Saver(
            tf.global_variables(),
            sharded=True,
            max_to_keep=keep_checkpoint_max,
            keep_checkpoint_every_n_hours=2,
            defer_build=False,
            save_relative_paths=True)
        tf.add_to_collection(tf.GraphKeys.SAVERS, saver)
        saver_listener = mtf.MtfCheckpointSaverListener(lowering)
        saver_hook = tf.train.CheckpointSaverHook(
            model_dir,
            save_steps=save_checkpoints_steps,
            saver=saver,
            listeners=[saver_listener])
        gin_config_saver_hook = gin.tf.GinConfigSaverHook(
            model_dir, summarize_config=True, include_step_in_filename=False)

        training_hooks = [
            restore_hook,
            saver_hook,
            gin_config_saver_hook,
        ]

        if use_tpu:
          return tpu_estimator.TPUEstimatorSpec(
              mode=tf_estimator.ModeKeys.TRAIN,
              loss=tf_loss,
              train_op=train_op,
              host_call=host_call,
              training_hooks=training_hooks)
        else:
          return tf_estimator.EstimatorSpec(
              tf_estimator.ModeKeys.TRAIN,
              loss=tf_loss,
              train_op=train_op,
              training_chief_hooks=training_hooks)
    elif mode == tf_estimator.ModeKeys.EVAL:
      # perplexity eval
      logits, loss = logits_and_loss(mtf_features)
      # compute cross-entropy while still on TPU to avoid having to outfeed the
      # logits, which might be big.
      logits = mtf.cast(logits, tf.float32)
      vocab_dim = logits.shape.dims[-1]
      targets = mtf_features["targets"]
      cross_entropy = mtf.layers.softmax_cross_entropy_with_logits(
          logits, targets, vocab_dim)
      anon_cross_entropy = mtf.anonymize(cross_entropy)
      predictions = mtf.cast(mtf.argmax(logits, vocab_dim), targets.dtype)
      anon_predictions = mtf.anonymize(predictions)
      anon_targets = mtf.anonymize(targets)
      # 0=padding and negative targets are a hack to indicate no loss
      anon_weights = mtf.cast(mtf.greater(anon_targets, 0), tf.float32)
      if model_type == "delimited_lm":
        anon_weights *= mtf.cast(
            mtf.logical_not(transformer.delimited_lm_inputs_mask(anon_targets)),
            dtype=tf.float32)

      lowering = mtf.Lowering(graph, {mesh: mesh_impl}, autostack=autostack)
      tf_loss = tf.cast(lowering.export_to_tf_tensor(loss), tf.float32)
      tf_loss = tf.cast(tf_loss, tf.float32)
      tf_predictions = lowering.export_to_tf_tensor(anon_predictions)
      tf_cross_entropy = lowering.export_to_tf_tensor(anon_cross_entropy)

      def simple_metrics(xent, predictions, labels, weights):
        """Simple metrics for teacher-forced eval."""
        token_correct = tf.cast(
            tf.equal(predictions, labels), tf.float32) * weights
        sequence_correct = tf.cast(
            tf.equal(tf.reduce_sum(token_correct, -1),
                     tf.reduce_sum(weights, -1)),
            tf.float32)
        sequence_weights = tf.cast(
            tf.not_equal(tf.reduce_sum(weights, -1), 0),
            tf.float32)
        # the purpose of "mean_label" is as a checksum to ensure that
        # models were evaluated on the same data.
        return {"neg_log_perplexity": tf.metrics.mean(-xent, weights),
                "token_accuracy": tf.metrics.mean(token_correct, weights),
                "sequence_accuracy": tf.metrics.mean(
                    sequence_correct, sequence_weights),
                "mean_label": tf.metrics.mean(
                    tf.cast(labels, tf.float32), weights),
                "num_eval_tokens": metric_sum(weights, name="num_eval_tokens"),
                "max_targets_length": metric_max(tf.reduce_sum(
                    weights, axis=-1), name="max_targets_length"),
               }

      labels = lowering.export_to_tf_tensor(anon_targets)
      weights = lowering.export_to_tf_tensor(anon_weights)
      eval_metrics = (simple_metrics, [
          tf_cross_entropy, tf_predictions, labels, weights])
      with mtf.utils.outside_all_rewrites():
        restore_hook = mtf.MtfRestoreHook(lowering)
      return tpu_estimator.TPUEstimatorSpec(
          tf_estimator.ModeKeys.EVAL,
          evaluation_hooks=[restore_hook],
          loss=tf_loss,
          eval_metrics=eval_metrics)

  return my_model_fn


def metric_sum(values, name=None, **kwargs):
  del kwargs
  with tf.variable_scope(name, "metric_sum", [values]):
    accum = tf.get_variable(
        "accum", shape=[], dtype=tf.float32, trainable=False,
        collections=[tf.GraphKeys.LOCAL_VARIABLES],
        initializer=tf.zeros_initializer())
    update_op = tf.assign_add(accum, tf.reduce_sum(tf.cast(values, tf.float32)))
    return accum, update_op


def metric_max(values, name=None, **kwargs):
  del kwargs
  with tf.variable_scope(name, "metric_max", [values]):
    accum = tf.get_variable(
        "accum", shape=[], dtype=tf.float32, trainable=False,
        collections=[tf.GraphKeys.LOCAL_VARIABLES],
        initializer=tf.zeros_initializer())
    update_op = tf.assign(
        accum, tf.maximum(accum, tf.reduce_max(tf.cast(values, tf.float32))))
    return accum, update_op


def _dynamic_text2self(mtf_features):
  """Convert a packed feature dictionary from text2text into text2self.

  This conversion is used when training a "delimited_lm" model.

  This allows us to train a text2self model on data that has been tokenized and
  packed in text2text format.

  Inputs and targets for each example get concatenated into the new targets.
  Length doubles.

  Args:
    mtf_features: a feature dictionary containing
       "inputs", "inputs_segmentation", "inputs_position",
       "targets", "targets_segmentation", "targets_position"
  Returns:
    a feature dictionary containing
      "targets", "targets_segmentation", "targets_position"
  """
  tf.logging.info(
      "_dynamic_text2self: Converting text2text problem to text2self")
  inputs = mtf_features["inputs"]
  targets = mtf_features["targets"]
  inputs_length_dim = inputs.shape.dims[-1]
  targets_length_dim = targets.shape.dims[-1]
  is_packed = "inputs_segmentation" in mtf_features
  if is_packed:
    inputs_segmentation = mtf_features["inputs_segmentation"]
    targets_segmentation = mtf_features["targets_segmentation"]
    inputs_position = mtf_features["inputs_position"]
    targets_position = mtf_features["targets_position"]
  else:
    inputs_segmentation = mtf.cast(
        mtf.not_equal(inputs, 0), tf.int32)
    targets_segmentation = mtf.cast(
        mtf.not_equal(targets, 0), tf.int32)
    inputs_position = mtf.range(
        inputs.mesh, inputs_length_dim, dtype=tf.int32) * inputs_segmentation
    targets_position = mtf.range(
        targets.mesh, targets_length_dim, dtype=tf.int32) * targets_segmentation
  # compute lengths of inputs and targets portions of each segment
  # segments_dim must be larger than the maximum number of segments.
  segments_dim = mtf.Dimension("segments", targets_length_dim.size)
  inputs_segment_length = mtf.reduce_sum(
      mtf.one_hot(inputs_segmentation, segments_dim, dtype=tf.int32),
      reduced_dim=inputs_length_dim)
  targets_segment_length = mtf.reduce_sum(
      mtf.one_hot(targets_segmentation, segments_dim, dtype=tf.int32),
      reduced_dim=targets_length_dim)
  # segment 0 means padding.  Zero out the segment lengths for segment 0.
  segments_range = mtf.range(targets.mesh, segments_dim, dtype=tf.int32)
  nonzero_segment = mtf.to_int32(mtf.not_equal(segments_range, 0))
  inputs_segment_length *= nonzero_segment
  targets_segment_length *= nonzero_segment
  combined_segment_length = inputs_segment_length + targets_segment_length
  # for targets, position in sequence increases by inputs_segment_length
  targets_position += mtf.gather(
      inputs_segment_length, targets_segmentation, segments_dim)
  # this is the new length dimension
  new_length_dim = mtf.Dimension(
      "new_length", inputs_length_dim.size + targets_length_dim.size)
  new_length_range = mtf.range(
      targets.mesh, new_length_dim, dtype=tf.int32)
  # compute permutation tensors mapping from the old length dimension to the
  # new length dimension
  combined_segment_length_cumulative = mtf.cumsum(
      combined_segment_length, segments_dim, exclusive=True)
  # segment 0 is padding - this causes it to get mapped out of range.
  combined_segment_length_cumulative += new_length_dim.size * mtf.to_int32(
      mtf.equal(segments_range, 0))
  inputs_destination = inputs_position + mtf.gather(
      combined_segment_length_cumulative, inputs_segmentation, segments_dim)
  inputs_permutation = mtf.to_int32(mtf.equal(
      new_length_range, inputs_destination))
  targets_destination = targets_position + mtf.gather(
      combined_segment_length_cumulative, targets_segmentation, segments_dim)
  targets_permutation = mtf.to_int32(mtf.equal(
      new_length_range, targets_destination))
  # map from the old length dimension to the new length dimension
  def _convert(t, perm):
    return mtf.rename_dimension(
        mtf.einsum([t, perm],
                   output_shape=inputs.shape.dims[:-1] + [new_length_dim]),
        "new_length", "length")
  targets = (
      _convert(inputs, inputs_permutation) +
      _convert(targets, targets_permutation))
  if is_packed:
    targets_segmentation = (
        _convert(inputs_segmentation, inputs_permutation) +
        _convert(targets_segmentation, targets_permutation))
    targets_position = (
        _convert(inputs_position, inputs_permutation) +
        _convert(targets_position, targets_permutation))
    return {
        "targets": targets,
        "targets_segmentation": targets_segmentation,
        "targets_position": targets_position,
    }
  else:
    return {"targets": targets}


def get_inputs_from_file(input_filename, ignore_comments=False):
  """Read data from file and strip new lines."""
  with tf.io.gfile.GFile(input_filename, "r") as f:
    inputs = [line.rstrip() for line in f]

  # If this is an empty file (because of stripping), return early.
  if not inputs:
    tf.logging.info("input file is empty after rstrip: %s", input_filename)
    return []

  # Strip the last empty line.
  if not inputs[-1]:
    inputs.pop()

  if ignore_comments:
    inputs = [l for l in inputs if not l.startswith("#")]

  return inputs


def encode_inputs(inputs,
                  vocabulary,
                  model_type,
                  batch_size,
                  sequence_length,
                  eos_id=1,
                  unscored_prefix=None):
  """Encode string inputs for inference/scoring.

  Args:
    inputs: list of strings
    vocabulary: a mtf.transformer.vocabulary.Vocabulary
    model_type: a string
    batch_size: an integer
    sequence_length: an integer (maximum decode length)
    eos_id: EOS id
    unscored_prefix: an optional list of strings

  Returns:
    all_input_ids: encoded inputs
  """
  n = len(inputs)
  all_input_ids = []
  for line_num, line in enumerate(inputs):
    ids = inputs_vocabulary(vocabulary).encode(line.strip())
    if unscored_prefix:
      prefix_str = unscored_prefix[line_num].strip()
      ids = [-i for i in inputs_vocabulary(vocabulary).encode(prefix_str)] + ids
    if model_type != "lm":
      # for text2self problems, the inputs represent a partial sequence
      # to be continued, and should not be terminated by EOS.
      # for sequence-to-sequence problems, the input needs to be EOS-terminated
      ids += [eos_id]
    if len(ids) > sequence_length:
      ids = ids[:sequence_length]
    else:
      ids.extend([0] * (sequence_length - len(ids)))
    all_input_ids.append(ids)
  # pad to make an integral number of batches
  all_input_ids.extend([all_input_ids[0]] * (-n % batch_size))
  all_input_ids = np.array(all_input_ids, dtype=np.int32)

  return all_input_ids


def encode_delimited_lm(inputs,
                        targets,
                        vocabulary,
                        batch_size,
                        sequence_length,
                        eos_id=1,
                        include_final_eos=True):
  """Encode inputs and targets for scoring a delimited langauge model.

  Args:
    inputs: list of strings
    targets: list of strings
    vocabulary: a mtf.transformer.vocabulary.Vocabulary
    batch_size: an integer
    sequence_length: an integer (maximum decode length)
    eos_id: EOS id
    include_final_eos: a boolean

  Returns:
    all_ids: encoded inputs
  """
  n = len(inputs)
  all_ids = []
  for inp, tgt in zip(inputs, targets):
    input_ids = inputs_vocabulary(vocabulary).encode(inp.strip()) + [eos_id]
    target_ids = targets_vocabulary(vocabulary).encode(tgt.strip())
    if include_final_eos:
      target_ids.append(eos_id)
    ids = input_ids + target_ids
    if len(ids) > sequence_length:
      ids = ids[:sequence_length]
    else:
      ids.extend([0] * (sequence_length - len(ids)))
    all_ids.append(ids)
  # pad to make an integral number of batches
  all_ids.extend([all_ids[0]] * (-n % batch_size))
  all_ids = np.array(all_ids, dtype=np.int32)
  return all_ids


@gin.configurable
def decode(estimator,
           input_fn,
           vocabulary,
           checkpoint_path=None):
  """Decode from an input_fn.

  Args:
    estimator: a TPUEstimator
    input_fn: function that returns a tf.Dataset
    vocabulary: a vocabulary.Vocabulary or (inputs_vocabulary,
      targets_vocabulary) tuple
    checkpoint_path: an optional string

  Yields:
    decoded strings
  """
  result_iter = estimator.predict(
      input_fn, checkpoint_path=checkpoint_path)

  def _maybe_detokenize(value, vocab):
    if isinstance(value, six.binary_type):
      return value
    return vocab.decode([int(x) for x in value])

  for i, result in enumerate(result_iter):
    input_string = _maybe_detokenize(
        result["inputs"], inputs_vocabulary(vocabulary))
    output_string = _maybe_detokenize(
        result["outputs"], targets_vocabulary(vocabulary))
    yield output_string
    if i & (i - 1) == 0:
      # LOG every power of 2.
      tf.logging.info("decoded %s: %s", i, input_string)
      tf.logging.info("            -> %s", output_string)


@gin.configurable
def compute_log_likelihoods(estimator,
                            input_fn,
                            checkpoint_path=None):
  """Decode from an input_fn.

  Args:
    estimator: a TPUEstimator
    input_fn: function that returns a tf.Dataset
    checkpoint_path: an optional string

  Returns:
    list of floats
  """
  result_iter = estimator.predict(
      input_fn, checkpoint_path=checkpoint_path)
  return [float(f) for f in result_iter]


def write_lines_to_file(lines, filename):
  """Write each line to a filename, replacing the file if it exists.

  Args:
    lines: list of str, lines to write out.
    filename: str, path to filename.
  """
  if tf.io.gfile.exists(filename):
    tf.io.gfile.remove(filename)
  tf.io.gfile.makedirs(os.path.dirname(filename))
  with tf.io.gfile.GFile(filename, "w") as output_file:
    for line in lines:
      output_file.write("{}\n".format(str(line).replace("\n", " ")))


def _get_combined_dataset_input_fn(
    datasets, batch_size, sequence_length, check_for_metrics=False):
  """Creates input function for estimator for inference, eval, and scoring.

  Args:
    datasets: A list of mesh_tensorflow.transformer.dataset.EvalDataset tuples.
      These will get combined together into a single tf.data.Dataset.
    batch_size: an integer
    sequence_length: an integer or a dict from feature-key to integer
      the (packed) sequence length, e.g. {"inputs": 512, "targets": 128}
    check_for_metrics: If True, then only include datasets which have associated
      metric functions.

  Returns:
    An input function for estimator.
  """
  def input_fn(params):
    """Input function for estimator."""
    del params

    combined_ds = None
    for dataset in datasets:
      if not check_for_metrics or dataset.metric_fns:
        ds = dataset.dataset_fn(sequence_length=sequence_length)
        ds = ds.map(
            _filter_features, num_parallel_calls=tf.data.experimental.AUTOTUNE)
        combined_ds = ds if not combined_ds else combined_ds.concatenate(ds)

    combined_ds = combined_ds.batch(batch_size, drop_remainder=False)
    # Pad the final batch.
    combined_ds = transformer_dataset.trim_and_pad_dataset(
        combined_ds, length=batch_size)
    combined_ds = combined_ds.prefetch(tf.data.experimental.AUTOTUNE)
    return combined_ds
  return input_fn


def get_step_from_checkpoint_path(checkpoint_path):
  """Returns the global step for the checkpoint at `checkpoint_path`.

  Assumes `checkpoint_path` corresponds to a file which contains the substring
  model.ckpt-{global_step}

  Args:
    checkpoint_path: str of path to a checkpoint file.

  Returns:
    int of the global step corresponding to the checkpoint file.

  Raises:
    ValueError if checkpoint_path does not correspond to a model checkpoint file
    which contains the global_step in its filename.
  """
  match = re.match(r".*model\.ckpt\-(\d+).*", checkpoint_path)
  if match is None:
    raise ValueError("Invalid checkpoint path {}".format(checkpoint_path))
  return int(match.group(1))


# TODO(noam): include more descriptive definitions
@gin.configurable
def decode_from_file(estimator,
                     vocabulary,
                     model_type,
                     batch_size,
                     sequence_length,
                     checkpoint_path=None,
                     input_filename=gin.REQUIRED,
                     output_filename=gin.REQUIRED,
                     eos_id=1,
                     repeats=1):
  """Decode from a text file and write to output_filename.

  Args:
    estimator: a TPUEstimator
    vocabulary: a mtf.transformer.vocabulary.Vocabulary
    model_type: a string
    batch_size: an integer
    sequence_length: an integer or a dict from feature-key to integer
      the (packed) sequence length, e.g. {"inputs": 512, "targets": 128}
    checkpoint_path: an optional string
    input_filename: a string
    output_filename: a string
    eos_id: EOS id
    repeats: an integer, the number of times to repeat each input.
  """
  inputs = get_inputs_from_file(input_filename)

  all_input_ids = encode_inputs(inputs, vocabulary, model_type, batch_size,
                                sequence_length["inputs"], eos_id=eos_id)
  def input_fn(params):
    del params
    dataset = tf.data.Dataset.from_tensor_slices({"inputs": all_input_ids})
    dataset = dataset.flat_map(
        lambda x: tf.data.Dataset.from_tensors(x).repeat(repeats))
    dataset = dataset.batch(batch_size, drop_remainder=True)
    dataset = dataset.prefetch(tf.data.experimental.AUTOTUNE)
    return dataset

  checkpoint_step = get_step_from_checkpoint_path(checkpoint_path)
  decodes = list(decode(
      estimator, input_fn, vocabulary, checkpoint_path=checkpoint_path))
  # Remove any padded examples
  dataset_size = len(inputs) * repeats
  decodes = decodes[:dataset_size]
  output_filename = "{}-{}".format(output_filename, checkpoint_step)
  write_lines_to_file(decodes, output_filename)


@gin.configurable
def decode_from_dataset(estimator,
                        vocabulary,
                        model_type,
                        batch_size,
                        sequence_length,
                        checkpoint_path=None,
                        infer_dataset_fn=gin.REQUIRED,
                        dataset_split="validation",
                        decode_output_dir=gin.REQUIRED):
  """Decode using inputs from the Task examples and writes results to files.

  Args:
    estimator: a TPUEstimator
    vocabulary: a mtf.transformer.vocabulary.Vocabulary
    model_type: a string
    batch_size: an integer
    sequence_length: an integer or a dict from feature-key to integer
      the (packed) sequence length, e.g. {"inputs": 512, "targets": 128}
    checkpoint_path: Checkpoint to use for inference.
    infer_dataset_fn: A function returning a list of dataset.EvalDataset tuples.
      See `eval_dataset_fn` argument to `eval_model` for details.
    dataset_split: str, which dataset split to load.
    decode_output_dir: a string, where to write inputs, targets, and decodes.
  """
  if model_type != "lm":
    raise ValueError("This function currently only supports decoder-only LMs.")

  infer_datasets = infer_dataset_fn(
      sequence_length=sequence_length,
      vocabulary=vocabulary,
      dataset_split=dataset_split,)

  input_fn = _get_combined_dataset_input_fn(
      infer_datasets, batch_size, sequence_length)

  checkpoint_step = get_step_from_checkpoint_path(checkpoint_path)
  # TODO(dei): Deal with case where decode() does not return the right number
  # of outputs. This can happen if the generator in decode() has failures.
  decodes = list(decode(
      estimator, input_fn, vocabulary, checkpoint_path=checkpoint_path))

  tf.logging.info("Caching inference examples.")
  with tf.Graph().as_default():
    for infer_dataset in infer_datasets:
      ds = infer_dataset.dataset_fn()

      # Create list of postprocessed text targets
      examples_for_ds = list(tfds.as_numpy(ds))
      examples_for_ds = _maybe_add_pretokenized_features(
          examples_for_ds, vocabulary)

      # Extract the portion of decodes corresponding to this dataset
      dataset_size = len(examples_for_ds)
      predictions = decodes[:dataset_size]

      # Remove the used decodes.
      del decodes[:dataset_size]

      # Write the predictions to file.
      predictions_filename = os.path.join(
          decode_output_dir,
          "{}_{}_predictions".format(infer_dataset.name, checkpoint_step),
      )
      write_lines_to_file(predictions, predictions_filename)

      # Write the ground-truth targets to file.
      targets = []
      for ex in examples_for_ds:
        targets_pretokenized = ex["targets_pretokenized"]
        targets.append(infer_dataset.postprocess_fn(
            targets_pretokenized, example=ex, is_target=True))
      targets_filename = os.path.join(
          decode_output_dir, "{}_targets".format(infer_dataset.name))
      write_lines_to_file(targets, targets_filename)

      # Write the inputs to a file.
      inputs = [ex["inputs_pretokenized"] for ex in examples_for_ds]
      inputs_filename = os.path.join(
          decode_output_dir, "{}_inputs".format(infer_dataset.name))
      write_lines_to_file(inputs, inputs_filename)


@gin.configurable
def clean_decodes(ids, eos_id=1, pad_id=0, length_axis=-1):
  """Replaces everything after EOS with PAD (along last axis).

  Args:
    ids: a d Tensor of type int.
    eos_id: int, EOS id.
    pad_id: int, PAD id.
    length_axis: an integer.

  Returns:
    a Tensor of type int of ids.
  """
  eos_and_after = tf.cumsum(tf.cast(tf.equal(ids, eos_id), tf.int32),
                            exclusive=True, axis=length_axis)
  valid_ids = tf.equal(eos_and_after, 0)
  return tf.where_v2(valid_ids, ids, pad_id)


@gin.configurable
def save_scores(results, vocabulary,
                scores_filename=None, save_example_text=True):
  """Processes results from scoring examples and maybe saves them to disk.

  Args:
    results: list of dictionaries containing the results for each scored
        example.
    vocabulary: a function that that returns a tf.data.Dataset with examples
      containing the string field 'targets' and optionally the field 'inputs'
    scores_filename: a string (path of file to write scores to). If None, scores
        are returned but not written to disk.
    save_example_text: a boolean - If True, then the text for each example is
        also saved/returned.

  Returns:
    List of float scores, one score per example. If save_example_text is True,
    the text of the inputs/targets for each example are also returned.
  """
  if not results:
    raise ValueError("No examples were scored.")

  scores = [r["scores"] for r in results]

  if scores_filename is not None:
    write_lines_to_file(["%g" % f for f in scores], scores_filename+".scores")

  if save_example_text:
    results = _maybe_add_pretokenized_features(results, vocabulary)

    # Targets will always exist.
    targets = [r.get("targets_pretokenized", r["targets"]) for r in results]
    if scores_filename is not None:
      write_lines_to_file(targets, scores_filename+".targets")

    # Write sequence lengths
    def get_sequence_length(tokens, pad_id=0):
      tokens = np.array(tokens)
      if not np.isin(pad_id, tokens):
        return len(tokens)
      # Argmax returns the index of the first occurrence of pad_id.
      return np.argmax(tokens == pad_id)

    seq_lengths = [get_sequence_length(r["targets"]) for r in results]
    if scores_filename is not None:
      write_lines_to_file(seq_lengths, scores_filename+".lengths")

    # Inputs may only exist for some tasks.
    if "inputs" in results[0]:
      inputs = [r.get("inputs_pretokenized", r["inputs"]) for r in results]
      if scores_filename is not None:
        write_lines_to_file(inputs, scores_filename+".inputs")
      return scores, inputs, targets
    else:
      return scores, targets

  return scores


@gin.configurable
def save_scores_to_tfrecords(
    results, vocabulary, scores_filename, shard_idx=0, save_ids_only=False):
  """Processes results from scoring examples and saves them to tfrecords files.

  Args:
    results: list of dictionaries containing the results for each scored
        example.
    vocabulary: a function that that returns a tf.data.Dataset with examples
      containing the string field 'targets' and optionally the field 'inputs'
    scores_filename: a string (path of file to write scores to).
    shard_idx: an integer indicating the current index of the file for sharding.
    save_ids_only: if true, save the ID that is prepended to the inputs,
      delimited by a space.
  """
  results = _maybe_add_pretokenized_features(results, vocabulary)
  scores = [r.get("scores", 0.0) for r in results]
  targets = [r.get("targets_pretokenized", r["targets"]) for r in results]
  inputs = [r.get("targets_neg_pretokenized",
                  r.get("inputs", "")) for r in results]

  if save_ids_only:
    inputs = [r.split(" ", 1)[0] for r in inputs]

  table_path = "{}_{}.tfrecord".format(scores_filename, shard_idx)
  tf.logging.info("Saving results to %s", table_path)

  with tf.io.TFRecordWriter(table_path) as file_writer:
    for input_, target, score in zip(inputs, targets, scores):
      record_bytes = tf.train.Example(
          features=tf.train.Features(
              feature={
                  "input":
                      tf.train.Feature(
                          bytes_list=tf.train.BytesList(
                              value=[bytes(input_, "utf8")])),
                  "target":
                      tf.train.Feature(
                          bytes_list=tf.train.BytesList(
                              value=[bytes(target, "utf8")])),
                  "score":
                      tf.train.Feature(
                          float_list=tf.train.FloatList(value=[score])),
              })).SerializeToString()
      file_writer.write(record_bytes)


@gin.configurable
def score_with_estimator(estimator, input_fn, eval_checkpoint_step, model_dir,
                         vocabulary, score_postprocess_fn=save_scores,
                         num_examples=None):
  """For each example returned by input_fn, compute log likelihood.

  Args:
    estimator: a TPUEstimator
    input_fn: a function that that returns a tf.data.Dataset with examples
      containing the string field 'targets' and optionally the field 'inputs'
    eval_checkpoint_step: int, list of ints, or None, see `eval_model`
      docstring.
    model_dir: string, estimator model_dir
    vocabulary: a vocabulary.Vocabulary or (inputs_vocabulary,
      targets_vocabulary) tuple
    score_postprocess_fn: a function that takes in model outputs and
      post-processes, saves, and returns them.
    num_examples: int, the total # of examples being scored, None if unknown

  Returns:
    a list of floats
  """
  checkpoint_path, = get_checkpoint_iterator(eval_checkpoint_step, model_dir)

  result_iter = estimator.predict(input_fn, checkpoint_path=checkpoint_path)
  # TODO(dei): This code is not well-designed for large-scale scoring, where the
  # number of examples might exceed available memory.
  results = list(result_iter)

  if num_examples is None:
    targets = [r["targets"] for r in results]
    num_padded = next((i for i, x in enumerate(targets[::-1]) if x.any()), None)
    num_examples = len(targets) - num_padded
  results = results[:num_examples]

  return score_postprocess_fn(results, vocabulary)


@gin.configurable
def score_with_estimator_lazy(
    estimator, input_fn, eval_checkpoint_step, model_dir,
    vocabulary, score_postprocess_fn=save_scores_to_tfrecords,
    num_examples=None, num_examples_per_shard=100000):
  """Score each example returned by input_fn lazily.

  Args:
    estimator: a TPUEstimator
    input_fn: a function that that returns a tf.data.Dataset with examples
      containing the string field 'targets' and optionally the field 'inputs'
    eval_checkpoint_step: int, list of ints, or None, see `eval_model`
      docstring.
    model_dir: string, estimator model_dir
    vocabulary: a vocabulary.Vocabulary or (inputs_vocabulary,
      targets_vocabulary) tuple
    score_postprocess_fn: a function that takes in model outputs
      post-processes, and saves them.
    num_examples: int, the total # of examples being scored, None if unknown
    num_examples_per_shard: int, the number of examples per file shard.

  Returns:
    a list of floats
  """
  if num_examples is not None:
    num_shards = math.ceil(num_examples / num_examples_per_shard)
  else:
    num_shards = None
  tf.logging.info("Scoring %s examples with %s shards at %s examples per shard",
                  num_examples, num_shards, num_examples_per_shard)

  checkpoint_path, = get_checkpoint_iterator(eval_checkpoint_step, model_dir)
  result_iter = estimator.predict(input_fn, checkpoint_path=checkpoint_path)

  start = time.time()
  results = []
  shard_idx = 0

  for i, result in enumerate(result_iter):
    results.append(result)
    num_results = len(results)
    exceeded_examples_per_shard = (
        num_examples_per_shard is not None
        and num_examples_per_shard > 0
        and num_results >= num_examples_per_shard)
    exceeded_num_examples = num_examples is not None and i >= num_examples

    if exceeded_examples_per_shard or exceeded_num_examples:
      score_postprocess_fn(results, vocabulary, shard_idx=shard_idx)

      elapsed = time.time() - start
      tf.logging.info("Scored %s results in %s s, %s examples/s for shard %s",
                      num_results, elapsed, num_results / elapsed, shard_idx)

      results = []
      shard_idx += 1
      start = time.time()

    if exceeded_num_examples:
      break

  if results:
    score_postprocess_fn(results, vocabulary, shard_idx=shard_idx)


def _maybe_add_pretokenized_features(examples, vocabulary):
  """Ensures decoded versions of "inputs" and "targets" exist in each example.

  Args:
    examples: List of example dictionaries containing mappings from feature
      name to np.array of integers.
    vocabulary: The vocabulary.

  Returns:
    examples dictionary with decoded plaintext entries for each feature in
    features that was present in the original example.
  """
  vocabulary = {"inputs": inputs_vocabulary(vocabulary),
                "targets": targets_vocabulary(vocabulary)}

  # This is just used for logging purposes.
  added_pretokenized = {"inputs": False, "targets": False}

  for example in examples:
    for feature_name in ["inputs", "targets"]:
      pretokenized_feature_name = feature_name + "_pretokenized"
      neg_pretokenized_feature_name = feature_name + "_neg_pretokenized"
      if feature_name in example and pretokenized_feature_name not in example:
        ids = example[feature_name].tolist()

        neg_ids = [abs(i) for i in ids if i < 0]
        ids = [i for i in ids if i > 0]

        decoded_string = vocabulary[feature_name].decode(ids)
        example[pretokenized_feature_name] = decoded_string

        if neg_ids:
          neg_decoded_string = vocabulary[feature_name].decode(neg_ids)
          example[neg_pretokenized_feature_name] = neg_decoded_string

        if not added_pretokenized[feature_name]:
          added_pretokenized[feature_name] = True
          tf.logging.warning(
              "Feature '%s' is being approximated by decoding from the "
              "tokenized feature '%s.'",
              pretokenized_feature_name, feature_name)
  return examples


@gin.configurable
def score_from_strings(estimator, vocabulary, model_type, batch_size,
                       sequence_length, model_dir, eval_checkpoint_step,
                       inputs=gin.REQUIRED, targets=gin.REQUIRED,
                       score_postprocess_fn=gin.REQUIRED, eos_id=1,
                       score_eos=True,
                       score_with_estimator_fn=score_with_estimator):
  """Compute log likelihoods per example and write to a text file.

  inputs & targets must either be the same length (in lines) or have inputs
  evenly divide targets N times, where each input has N decodes sequentially
  in targets.

  The function returns a list of floats represnenting the log-liekelihood of the
  target given the input.  If `scores_filename` is present, then these are also
  written out as a text file, one per line.

  Args:
    estimator: a TPUEstimator
    vocabulary: a mtf.transformer.vocabulary.Vocabulary
    model_type: a string
    batch_size: an integer
    sequence_length: an integer or a dict from feature-key to integer
      the (packed) sequence length, e.g. {"inputs": 512, "targets": 128}
    model_dir: string, estimator model_dir
    eval_checkpoint_step: int, list of ints, or None, see `eval_model`
      docstring.
    inputs: optional - a list of strings (inputs) the same length as targets
      alternatively, a string filepath for a text file (one string per line)
    targets: a list of strings (targets)
      alternatively, a string filepath for a text file (one string per line)
    score_postprocess_fn: Function that takes in model outputs and
      post-processes then returns then.
    eos_id: EOS id
    score_eos: a boolean - whether to score the final eos token of each line
      If this is set to false, the scores can be interpreted as prefix
      log-likelihoods
    score_with_estimator_fn: a function to run scoring with the estimator.
  Returns:
    a list of floats
  """
  if isinstance(inputs, str):
    inputs = get_inputs_from_file(inputs)
  if isinstance(targets, str):
    targets = get_inputs_from_file(targets)
  has_inputs = inputs is not None
  if has_inputs:
    if len(inputs) < len(targets):
      # We assume that the targets file contains n targets for each input.
      # So we repeat each input n times.
      if len(targets) % len(inputs):
        raise ValueError("len(inputs) must divide len(targets), got %d and %d"
                         % (len(inputs), len(targets)))
      repeats = len(targets) // len(inputs)
      inputs = [inputs[i // repeats] for i in range(len(targets))]
    elif len(targets) < len(inputs):
      # `targets` is a list of one string.  Use it as a target for all inputs.
      if len(targets) != 1:
        raise ValueError("Expected only one target string")
      targets = targets * len(inputs)
  if has_inputs and model_type == "lm":
    has_inputs = False
    all_target_ids = encode_inputs(
        targets, vocabulary, model_type, batch_size,
        sequence_length["targets"], eos_id=eos_id if score_eos else 0,
        unscored_prefix=inputs)
  else:
    if has_inputs:
      all_input_ids = encode_inputs(inputs, vocabulary, model_type, batch_size,
                                    sequence_length["inputs"], eos_id=eos_id)
    all_target_ids = encode_inputs(
        targets, vocabulary, model_type, batch_size,
        sequence_length["targets"], eos_id=eos_id if score_eos else 0)

  def input_fn(params):
    del params
    m = ({"inputs": all_input_ids, "targets": all_target_ids} if has_inputs
         else {"targets": all_target_ids})
    dataset = tf.data.Dataset.from_tensor_slices(m)
    dataset = dataset.flat_map(tf.data.Dataset.from_tensors)
    dataset = dataset.batch(batch_size, drop_remainder=True)
    return dataset.prefetch(tf.data.experimental.AUTOTUNE)

  return score_with_estimator_fn(
      estimator, input_fn, eval_checkpoint_step, model_dir,
      vocabulary, score_postprocess_fn, len(targets))


@gin.configurable
def score_from_dataset(estimator, vocabulary, batch_size, sequence_length,
                       model_dir, eval_checkpoint_step, dataset_split,
                       score_dataset_fn=None,
                       score_postprocess_fn=gin.REQUIRED,
                       score_with_estimator_fn=score_with_estimator):
  """Compute log likelihoods per example and write to a text file.

  The function returns a list of floats representing the log-likelihood of the
  target given the input.  If `scores_filename` is present, then these are also
  written out as a text file, one per line. If multiple datasets are returned,
  their scores will be concatenated.

  Args:
    estimator: a TPUEstimator
    vocabulary: a mtf.transformer.vocabulary.Vocabulary
    batch_size: an integer
    sequence_length: an integer or a dict from feature-key to integer
      the (packed) sequence length, e.g. {"inputs": 512, "targets": 128}
    model_dir: string, estimator model_dir
    eval_checkpoint_step: int, list of ints, or None, see `eval_model`
      docstring.
        dataset_split: a string
    score_dataset_fn: A function returning a list of dataset.EvalDataset tuples.
      See `eval_dataset_fn` argument to `eval_model` for details.
    score_postprocess_fn: Function that takes in model outputs and
      post-processes then returns then.
    score_with_estimator_fn: a function to run scoring with the estimator.

  Returns:
    scores: a list of floats, the log likelihood scores
    targets: a list of strings, scored targets
  """
  scoring_datasets = score_dataset_fn(
      sequence_length=sequence_length,
      vocabulary=vocabulary,
      dataset_split=dataset_split)

  input_fn = _get_combined_dataset_input_fn(
      scoring_datasets, batch_size, sequence_length)

  return score_with_estimator_fn(
      estimator, input_fn, eval_checkpoint_step, model_dir,
      vocabulary, score_postprocess_fn)


def get_estimator(model_type, vocabulary, mesh_shape,
                  layout_rules, model_dir, batch_size, sequence_length,
                  autostack, learning_rate_schedule, keep_checkpoint_max,
                  save_checkpoints_steps, optimizer, predict_fn,
                  variable_filter, ensemble_inputs, use_tpu, tpu_job_name,
                  iterations_per_loop, cluster, init_checkpoint=None,
                  mesh_devices=None, score_in_predict_mode=False):
  """Create TPU estimator for the transfomer Mesh-TF model.

  Args:
    model_type: a string - either "bitransformer", "bi_student_teacher", lm" or
      "aligned"
    vocabulary: a vocabulary.Vocabulary or (inputs_vocabulary,
      targets_vocabulary) tuple
    mesh_shape: a function passed in through gin that returns a mtf.Shape
    layout_rules: an input to mtf.convert_to_layout_rules()
    model_dir: a string, model directory path.
    batch_size: an integer, global batch size.
    sequence_length: a dict, see `train_model` docstring for details.
    autostack: boolean, internally combine variables
    learning_rate_schedule: an optional function taking the scalar name argument
      `step` and the numeric argument `total_train_steps` and return the scalar
      learning rate
    keep_checkpoint_max: an integer, maximum number of checkpoints to keep
    save_checkpoints_steps: integer, steps per checkpoint
    optimizer: a class extending optimize.Optimizer, required for training
    predict_fn: an optional function that can be used to override the default
      transformer prediction behavior. Must return a tensor of shape [batch_dim,
      length_dim] that will be the prediction for each example. Must accept the
      following arguments:
        - model: a Unitransformer or Bitransformer
        - features: a dict representing an example. Every value will be an
          mtf.Tensor with shape [batch_dim, length_dim].
        - variable_dtype: an mtf.VariableDType
    variable_filter: a string, a variable will only be trained if its name
      matches this regex. If None (default), train all trainable variables.
    ensemble_inputs: an integer, see `train_model` docstring for details.
    use_tpu: string, the Cloud TPU to use for training
    tpu_job_name: string, name of TPU worker binary
    iterations_per_loop: integer, steps per train loop
    cluster: a TPUClsuterResolver object
    init_checkpoint: a string, if not None then read in variables from this
      checkpoint path when initializing variables. Will only initialize
      variables that appear both in the current graph and the checkpoint.
    mesh_devices: a list of strings, the device names to use for each mesh
      slice. Only required for GPU.
    score_in_predict_mode: a bool, compute log-likelihood scores instead of
      predictions.
  Returns:
    an Estimator object.
  """
  my_tpu_config = tpu_config.TPUConfig(
      tpu_job_name=tpu_job_name,
      iterations_per_loop=iterations_per_loop,
      num_cores_per_replica=1,
      per_host_input_for_training=tpu_config.InputPipelineConfig.BROADCAST,
  )

  session_config = None
  if use_tpu:
    # meta-optimizer drastically slows down startup time and has little benefit
    # when running on TPU.
    session_config = tf.ConfigProto(
        graph_options=tf.GraphOptions(
            rewrite_options=rewriter_config_pb2.RewriterConfig(
                disable_meta_optimizer=True)))

  run_config = tpu_config.RunConfig(
      cluster=cluster,
      model_dir=model_dir,
      tpu_config=my_tpu_config,
      session_config=session_config,
      save_checkpoints_steps=save_checkpoints_steps,
      save_checkpoints_secs=None)

  transformer_model = build_model(
      model_type=model_type,
      input_vocab_size=inputs_vocabulary(vocabulary).vocab_size,
      output_vocab_size=targets_vocabulary(vocabulary).vocab_size,
      layout_rules=layout_rules,
      mesh_shape=mesh_shape)

  model_fn = tpu_estimator_model_fn(
      model_type=model_type,
      transformer_model=transformer_model,
      vocabulary=vocabulary,
      model_dir=model_dir,
      use_tpu=use_tpu,
      mesh_shape=mesh_shape,
      layout_rules=layout_rules,
      batch_size=batch_size,
      sequence_length=sequence_length,
      autostack=autostack,
      learning_rate_schedule=learning_rate_schedule,
      keep_checkpoint_max=keep_checkpoint_max,
      save_checkpoints_steps=save_checkpoints_steps,
      optimizer=optimizer,
      predict_fn=predict_fn,
      variable_filter=variable_filter,
      ensemble_inputs=ensemble_inputs,
      init_checkpoint=init_checkpoint,
      mesh_devices=mesh_devices,
      score_in_predict_mode=score_in_predict_mode)

  estimator = tpu_estimator.TPUEstimator(
      model_fn=model_fn,
      config=run_config,
      train_batch_size=batch_size,
      eval_batch_size=batch_size,
      predict_batch_size=batch_size,
      use_tpu=use_tpu,
      export_to_tpu=False,
      params={})

  return estimator


def train_model(estimator, vocabulary, sequence_length, batch_size,
                train_dataset_fn, train_steps, ensemble_inputs,
                dataset_split="train", skip_seen_data=False,
                seen_data_init_step=0, checkpoint_input_pipeline=False):
  """Train a Mesh-TF model.

  Args:
    estimator: Estimator object, created with the appropriate model_fn.
    vocabulary: a vocabulary.Vocabulary or (inputs_vocabulary,
      targets_vocabulary) tuple
    sequence_length: a dict from feature-key to integer the (packed)
      sequence length, e.g. {"inputs": 512, "targets": 128}
    batch_size: an integer, global batch size
    train_dataset_fn: A function returning a tf.data.Dataset. Should accept the
     following arguments:
      - sequence_length: an integer or a dict from feature-key to integer
        the (packed) sequence length, e.g. {"inputs": 512, "targets": 128}
      - vocabulary: Vocabulary instance to use for encoding.
      - dataset_split: str, which dataset split to load.
    train_steps: an integer, number of steps for training.
    ensemble_inputs: an optional integer - pass the size of the ensemble to
      train an ensemble where each model gets different inputs. You also need to
      configure Unitransformer.ensemble  to the right size. If None, then all
      models are trained on the same inputs.
    dataset_split: str, which dataset split to train on.
    skip_seen_data: a boolean, is `False` by default. Used when a training run
      restarts to skip already seen data. This flag is only consistent when
      every setting (such as batch size and random seed) on the model is the
      same between the original run and the new run. May require a significant
      amount of time to skip a large number of steps.
    seen_data_init_step: an integer, when `skip_seen_data` is True, skip seen
      steps from this starting point. Useful when finetuning.
    checkpoint_input_pipeline: a boolean, whether to checkpoint the input
      pipeline in order to restart from the previous run. May require a large
      amount of disk space for complicated input pipelines.
  """

  if skip_seen_data and checkpoint_input_pipeline:
    raise ValueError(
        "At most one of `skip_seen_data` and `checkpoint_input_pipeline` may "
        "be set.")

  def input_fn(params):
    del params

    dataset = train_dataset_fn(
        sequence_length=sequence_length,
        vocabulary=vocabulary,
        dataset_split=dataset_split)
    dataset = dataset.repeat().batch(
        batch_size * (ensemble_inputs or 1), drop_remainder=True)
    dataset = dataset.prefetch(tf.data.experimental.AUTOTUNE)

    # On the first time data is read in after relaunching, skip data that has
    # already been seen.
    if skip_seen_data and estimator.latest_checkpoint() is not None:
      recovered_step = estimator.get_variable_value("global_step")
      steps_to_skip = recovered_step - seen_data_init_step
      if steps_to_skip > 0:
        tf.logging.info("Skipping %d steps of data.", steps_to_skip)
        dataset = dataset.skip(steps_to_skip)
    return dataset

  hooks = []
  if checkpoint_input_pipeline:
    hooks.append(tf.data.experimental.CheckpointInputPipelineHook(estimator))

  estimator.train(input_fn=input_fn, max_steps=train_steps, hooks=hooks)


@gin.configurable
def infer_model(estimator,
                vocabulary,
                sequence_length,
                batch_size,
                model_type,
                model_dir,
                eval_checkpoint_step,
                checkpoint_paths=None,
                decode_fn=decode_from_file):
  """Infer a Mesh-TF model.

  Args:
    estimator: Estimator object, created with the appropriate model_fn.
    vocabulary: a vocabulary.Vocabulary or (inputs_vocabulary,
      targets_vocabulary) tuple
    sequence_length: a dict from feature-key to integer the (packed)
      sequence length, e.g. {"inputs": 512, "targets": 128}
    batch_size: an integer, global batch size
    model_type: a string - either "bitransformer", "bi_student_teacher", lm" or
      "aligned"
    model_dir: string, estimator model_dir
    eval_checkpoint_step: int, list of ints, or None, see `eval_model`
      docstring.
    checkpoint_paths: optional list of checkpoints to run inference for
    decode_fn: decoding function, defaults to decode_from_file
  """
  if checkpoint_paths is None:
    checkpoint_paths = get_checkpoint_iterator(eval_checkpoint_step, model_dir)

  for checkpoint_path in checkpoint_paths:
    decode_fn(
        estimator,
        vocabulary=vocabulary,
        model_type=model_type,
        batch_size=batch_size,
        sequence_length=sequence_length,
        checkpoint_path=checkpoint_path)


def eval_model(estimator,
               vocabulary,
               sequence_length,
               batch_size,
               dataset_split,
               model_dir,
               eval_dataset_fn,
               eval_summary_dir,
               eval_checkpoint_step,
               eval_with_score=False,
               output_eval_examples=True,
               eval_dir_suffix=None,
               score_with_estimator_fn=score_with_estimator):
  """Eval a Mesh-TF model.

  Args:
    estimator: an Estimator object or a callable that returns one.
    vocabulary: a vocabulary.Vocabulary or (inputs_vocabulary,
      targets_vocabulary) tuple
    sequence_length: a dict from feature-key to integer the (packed)
      sequence length, e.g. {"inputs": 512, "targets": 128}. May also be set to
      `None` to automatically compute the maximum length of the examples, which
      requires `estimator` to be a callable.
    batch_size: an integer, global batch size
    dataset_split: a string
    model_dir: a string, directory with the model.
    eval_dataset_fn: A function returning a list of dataset.EvalDataset tuples.
      Must be provided for mode="eval". Should accept the following arguments:
        - sequence_length: an integer or a dict from feature-key to integer
          the (packed) sequence length, e.g. {"inputs": 512, "targets": 128}
        - vocabulary: Vocabulary instance to use for encoding.
        - dataset_split: str, which dataset split to load.
      dataset.EvalDataset tuples are namedtuples with the following fields:
        - name: string, the task name
        - dataset_fn: function which returns a tf.data.Dataset of tokenized and
          padded examples. Must not require any arguments and must include the
          feature keys 'inputs' and 'targets_pretokenized'.
        - postprocess_fn: function which converts original targets to values
          that can be processed by a `metric_fn`.
        - list_of_metric_fns: list of metric functions with the call signature
          `metric_fn(targets, predictions)` which returns a dict mapping
          submetric names to scalar values. TensorBoard summaries and other tags
          will be written out using the submetric names.
    eval_summary_dir: str, path to write TensorBoard events file summaries for
      eval. If None, use model_dir/eval_{split}.
    eval_checkpoint_step: int, list of ints, or None. If an int or list of ints,
      evaluation or inference will be run on the checkpoint files in `model_dir`
      whose global steps are closest to the global steps provided. If None and
      mode="eval", run eval continuously waiting for new checkpoints via
      `tf.train.checkpoints_iterator`.
    eval_with_score: bool, whether to evaluate using log likelihood scores of
      targets instead of decoded predictions.
    output_eval_examples: bool, whether to dump inputs, targets and predictions
      of the eval examples in plaintext to eval_summary_dir.
    eval_dir_suffix: string, if not None then will appended to the
      eval_summary_dir.
    score_with_estimator_fn: a function to run scoring with the estimator.
  """
  if eval_dataset_fn is None:
    raise ValueError("Must provide eval_dataset_fn through gin for eval.")
  if sequence_length is None and not callable(estimator):
    raise ValueError(
        "A callable must be passed for the estimator when automatically "
        "computing the sequence length.")

  eval_datasets = eval_dataset_fn(
      sequence_length=sequence_length,
      vocabulary=vocabulary,
      dataset_split=dataset_split,
  )

  valid_eval_datasets = []
  for eval_dataset in eval_datasets:
    if not eval_dataset.metric_fns:
      tf.logging.info("Skipping %s because metric_fns is empty",
                      eval_dataset.name)
      continue
    # Convert to EvalDataset tuple in case eval_dataset_fn returns raw tuples
    valid_eval_datasets.append(transformer_dataset.EvalDataset(*eval_dataset))
  eval_datasets = valid_eval_datasets

  if not eval_datasets:
    tf.logging.info(
        "All provided EvalDatasets have metric_fns=[]; eval is not possible.")
    return

  eval_summary_dir = eval_summary_dir or os.path.join(
      model_dir, "{}_eval".format(dataset_split))
  if eval_dir_suffix is not None:
    eval_summary_dir += "_{}".format(eval_dir_suffix)
  summary_writer = tf.summary.FileWriter(eval_summary_dir)

  # Pre-load in all of the targets once before entering continuous eval loop
  cached_targets = {}
  cached_examples = {}
  # Need to create a separate graph for loading in original targets
  # or else TF will complain that we modified the graph
  max_sequence_length = {"inputs": 0, "targets": 0}

  tf.logging.info("Caching evaluation examples.")
  with tf.Graph().as_default():
    for eval_dataset in eval_datasets:
      if eval_dataset.metric_fns:
        ds = eval_dataset.dataset_fn()
        # Create list of postprocessed text targets
        inputs = []
        targets = []
        examples = []
        for ex in tfds.as_numpy(ds):
          max_sequence_length["inputs"] = max(
              max_sequence_length["inputs"], len(ex["inputs"]))
          max_sequence_length["targets"] = max(
              max_sequence_length["targets"], len(ex["targets"]))
          examples.append(ex)
          if "inputs_pretokenized" in ex:
            inputs.append(ex["inputs_pretokenized"])
          if "targets_pretokenized" in ex:
            targets_pretokenized = ex["targets_pretokenized"]
            if isinstance(targets_pretokenized, bytes):
              targets_pretokenized = targets_pretokenized.decode("utf-8")
            targets.append(
                eval_dataset.postprocess_fn(
                    targets_pretokenized, example=ex, is_target=True)
            )
        if output_eval_examples:
          targets_filename = os.path.join(
              eval_summary_dir,
              "{}_targets".format(eval_dataset.name),
          )
          write_lines_to_file(targets, targets_filename)
          inputs_filename = os.path.join(eval_summary_dir,
                                         "{}_inputs".format(eval_dataset.name))
          write_lines_to_file(inputs, inputs_filename)

        cached_targets[eval_dataset.name] = targets
        cached_examples[eval_dataset.name] = examples
  if sequence_length is None:
    tf.logging.info("Setting sequence lengths to %s", max_sequence_length)
    sequence_length = max_sequence_length
    estimator = functools.partial(estimator, sequence_length=sequence_length)
  elif (sequence_length["inputs"] < max_sequence_length["inputs"] or
        sequence_length["targets"] < max_sequence_length["targets"]):
    tf.logging.warning(
        "Given sequence lengths are insufficient for some evaluation inputs or "
        "targets. These sequences will be truncated to fit, likely leading to "
        "sub-optimal results. Consider passing `None` for sequence_length to "
        "have them be automatically computed.\n Got: %s,\n Max Lengths: %s",
        sequence_length, max_sequence_length)
  elif (sequence_length["inputs"] > max_sequence_length["inputs"] or
        sequence_length["targets"] > max_sequence_length["targets"]):
    tf.logging.warning(
        "Given sequence lengths are longer than necessary for some evaluation "
        "inputs or targets, resulting in wasted computation. Consider passing "
        "`None` for sequence_length to have them be automatically computed.\n"
        " Got: %s,\n Max Lengths: %s",
        sequence_length, max_sequence_length)

  if callable(estimator):
    estimator = estimator()

  input_fn = _get_combined_dataset_input_fn(
      eval_datasets, batch_size, sequence_length, check_for_metrics=True)

  checkpoint_paths = get_checkpoint_iterator(eval_checkpoint_step, model_dir)
  for checkpoint_path in checkpoint_paths:
    tf.logging.info("Checkpoint path %s", checkpoint_path)
    global_step = int(get_step_from_checkpoint_path(checkpoint_path))
    if eval_with_score:
      outputs, _ = score_with_estimator_fn(
          estimator, input_fn, global_step, model_dir, vocabulary,
          num_examples=sum(len(cex) for cex in cached_examples.values()))
    else:
      outputs = [
          d.decode("utf-8") if isinstance(d, bytes) else d
          for d in decode(estimator, input_fn, vocabulary, checkpoint_path)
      ]
    for eval_dataset in eval_datasets:
      # Extract the portion of decodes corresponding to this dataset
      examples = cached_examples[eval_dataset.name]
      dataset_size = len(examples)
      predictions = [
          eval_dataset.postprocess_fn(d, example=ex)
          for d, ex in zip(outputs[:dataset_size], examples)
      ]
      # Remove the used decodes.
      del outputs[:dataset_size]

      global_step = int(get_step_from_checkpoint_path(checkpoint_path))

      if output_eval_examples:
        predictions_filename = os.path.join(
            eval_summary_dir,
            "{}_{}_predictions".format(eval_dataset.name, global_step),
        )
        write_lines_to_file(predictions, predictions_filename)

      for metric_fn in eval_dataset.metric_fns:
        summary = tf.Summary()
        targets = cached_targets[eval_dataset.name]
        metric_result = metric_fn(targets, predictions)
        if isinstance(metric_result, tf.Summary):
          tf.logging.info("Precomputed summary at step %d", global_step)
          summary_writer.add_summary(metric_result, global_step)
        else:
          for metric_name, metric_value in metric_result.items():
            tag = "eval/{}/{}".format(eval_dataset.name, metric_name)
            tf.logging.info("%s at step %d: %.3f", tag, global_step,
                            metric_value)
            summary.value.add(tag=tag, simple_value=metric_value)
          summary_writer.add_summary(summary, global_step)
      summary_writer.flush()

    # Only padding should remain.
    expected_pad = -sum(len(t) for t in cached_targets.values()) % batch_size
    if outputs and len(outputs) != expected_pad:
      raise ValueError("{} padded outputs, {} expected.".format(
          len(outputs), expected_pad))


def export_model(estimator, export_dir, vocabulary, sequence_length,
                 model_type, eval_with_score=False, batch_size=1,
                 checkpoint_path=None):
  """Export a model in TF SavedModel format to be used for inference on CPUs.

  Args:
    estimator: Estimator object, estimator created with the appropriate
      model_fn.
    export_dir: str, a directory in which to create timestamped subdirectories
      containing exported SavedModels.
    vocabulary: sentencepiece vocab, vocabulary instance to use for encoding.
    sequence_length: an integer or a dict from feature-key to integer
      the (packed) sequence length, e.g. {"inputs": 512, "targets": 128}
    model_type: a string, see `get_estimator` docstring for details.
    eval_with_score: If True, compute log-likelihood scores of targets.
      If False, do inference to generate outputs.
    batch_size: int, number of sequences per batch. Should match estimator.
    checkpoint_path: str, path to checkpoint. If None (default), use the most
      recent in the model directory.

  Returns:
    The string path to the exported directory.
  """

  def serving_input_fn():
    """Constructs input portion of Graph in serving.

    Input is a batch of strings.

    Returns:
      a ServingInputReceiver
    """

    def str_placeholder(name):
      return tf.placeholder(dtype=tf.string, shape=[None], name=name)

    if model_type == "lm" or not eval_with_score:
      # In this case, users of exported model provide only one feature, which is
      # "targets" if scoring or "inputs" if doing prediction.

      input_key = "targets" if eval_with_score else "inputs"
      vocab_to_use = (targets_vocabulary(vocabulary) if eval_with_score
                      else inputs_vocabulary(vocabulary))
      targets = str_placeholder(input_key)

      predict_batch_size = tf.shape(targets)[0]
      dataset = tf.data.Dataset.from_tensor_slices({input_key: targets})
      dataset = transformer_dataset.encode_all_features(dataset, vocab_to_use)

      receiver_tensors = {input_key: targets}
    else:
      # When scoring for encoder-decoder models, both "inputs" and "targets"
      # must be provided.

      inputs = str_placeholder("inputs")
      targets = str_placeholder("targets")

      predict_batch_size = tf.shape(inputs)[0]

      inputs_dataset = transformer_dataset.encode_all_features(
          tf.data.Dataset.from_tensor_slices({"inputs": inputs}),
          inputs_vocabulary(vocabulary))

      targets_dataset = transformer_dataset.encode_all_features(
          tf.data.Dataset.from_tensor_slices({"targets": targets}),
          targets_vocabulary(vocabulary))

      dataset = tf.data.Dataset.zip((inputs_dataset, targets_dataset))
      dataset = dataset.map(lambda x, y: {**x, **y},
                            num_parallel_calls=tf.data.experimental.AUTOTUNE)

      receiver_tensors = {"inputs": inputs, "targets": targets}

    dataset = transformer_dataset.pack_or_pad(
        dataset=dataset,
        length=sequence_length,
        pack=False,
        feature_keys=receiver_tensors.keys()
    )

    # Batch, and pad final batch.
    tf.debugging.assert_less_equal(predict_batch_size, batch_size)
    dataset = dataset.batch(batch_size, drop_remainder=False)
    dataset = transformer_dataset.trim_and_pad_dataset(
        dataset, length=batch_size)

    features = tf.data.experimental.get_single_element(dataset)
    features["predict_batch_size"] = predict_batch_size
    return tf_estimator.export.ServingInputReceiver(
        features=features, receiver_tensors=receiver_tensors)

  return estimator.export_saved_model(
      export_dir, serving_input_fn, checkpoint_path=checkpoint_path)


def compute_batch_size(sequence_length,
                       mesh_shape,
                       layout_rules,
                       method_and_value):
  """Compute the total batch size in sequences.

  method_and_value is a (string, int) pair.
  The method string is one of the following four options:

  "sequences_per_batch"
  "tokens_per_batch"
  "sequences_per_replica"
  "tokens_per_replica"

  According to the method string, the value represents either a number of
  sequences or a number of tokens, and represents either the size of the total
  batch or the fraction of the batch assigned to each model replica.

  For example ("tokens_per_replica", 2048) means that the batch size should be
  set so that the number of tokens per model replica is 2048.  So if the
  sequence length is 1024 and there is 16-way data-parallelism, then the number
  of sequences per batch would be 2048 * 16 / 1024 = 32.

  The "per_batch" versions are useful for ensuring indentical overall batch
  sizes across different mesh shapes/layouts.  The "per_replica" versions are
  useful for scaling up the total batch size relative to the degree of
  data-parallelism

  Args:
    sequence_length: an integer or a dict from feature-key to integer
      the (packed) sequence length, e.g. {"inputs": 512, "targets": 128}
    mesh_shape: an input to mtf.convert_to_shape()
    layout_rules: an input to mtf.convert_to_layout_rules()
    method_and_value: a pair
  Returns:
    an integer - the number of sequences per batch
  """
  def checkdiv(a, b):
    if a % b:
      raise ValueError("%d is not divisible by %d" % (a, b))
    return a // b
  num_replicas = (
      mtf.tensor_dim_to_mesh_dim_size(
          layout_rules, mesh_shape, mtf.Dimension("batch", 0)) *
      mtf.tensor_dim_to_mesh_dim_size(
          layout_rules, mesh_shape, mtf.Dimension("outer_batch", 0)))
  method, value = method_and_value
  if method == "sequences_per_batch":
    return value
  sequence_length = max(sequence_length.values())
  if method == "tokens_per_batch":
    return checkdiv(value, sequence_length)
  elif method == "sequences_per_replica":
    return value * num_replicas
  elif method == "tokens_per_replica":
    return checkdiv(value, sequence_length) * num_replicas
  else:
    raise ValueError("unknown method %s" % method,)


@gin.configurable
def serialize_num_microbatches(batch_dim,
                               sequence_length,
                               mesh_shape,
                               layout_rules,
                               tokens_per_microbatch_per_replica=None):
  """Number of microbatches per batch for serialized training.

  We want to split each training step into multiple sequential steps
  to limit memory usage.  Gradients are accumulated locally and reduced once.

  This function determines the number of microbatches per batch.
  If tokens_per_microbatch_per_replica=None, then the batch is not split.

  Args:
    batch_dim: a mtf.Dimension
    sequence_length: an integer or a dict from feature-key to integer
      the (packed) sequence length, e.g. {"inputs": 512, "targets": 128}
    mesh_shape: an input to mtf.convert_to_shape()
    layout_rules: an input to mtf.convert_to_layout_rules()
    tokens_per_microbatch_per_replica: an optional integer, e.g. 2048
  Returns:
    an integer
  """
  if not tokens_per_microbatch_per_replica:
    return 1
  batch_per_replica = mtf.tensor_dim_to_size_per_split(
      layout_rules, mesh_shape, batch_dim)
  # number of sequences per microbatch
  microbatch_size = max(
      1, tokens_per_microbatch_per_replica // max(sequence_length.values()))
  # decrease microbatch_size until it is a divisor of batch_per_replica
  # This is guaranteed to stop at microbatch_size=1 if not earlier.
  while batch_per_replica % microbatch_size:
    microbatch_size -= 1
  num_microbatches = batch_per_replica // microbatch_size
  tf.logging.info(
      "serialize_num_microbatches: "
      "tokens_per_microbatch_per_replica=%d "
      "batch_dim=%s "
      "sequence_length=%s "
      "batch_per_replica=%d "
      "num_microbatches=%d",
      tokens_per_microbatch_per_replica,
      batch_dim,
      sequence_length,
      batch_per_replica,
      num_microbatches)
  return int(num_microbatches)


@gin.configurable
def auto_train_steps(batch_size,
                     sequence_length,
                     train_tokens=2 ** 36):
  """Automatically compute number of training steps.

  Since the batch size and sequence length can vary across experiments, we
  specify the amount of training in terms of (non-unique) input tokens processed
  over the course of training the model.  The number of steps is computed as

    train_steps = train_tokens // (batch_size * sequence_length)

  Args:
    batch_size: an integer
    sequence_length: an integer or a dict from feature-key to integer
      the (packed) sequence length, e.g. {"inputs": 512, "targets": 128}
    train_tokens: an integer (train_steps * batch_size * sequence_length)
  Returns:
    an integer
  """
  return train_tokens // (batch_size * max(sequence_length.values()))


@gin.configurable
def get_checkpoint_iterator(checkpoint_step, model_dir, skip_until=0,
                            stop_after=None, find_closest=True):
  """Get an iterable of checkpoint paths from a provided checkpoint step(s).

  Args:
    checkpoint_step: If checkpoint_step is an int, return a singleton list with
      that checkpoint path in it. If find_closest, the checkpoint with the
      closest global step will be reurned. If checkpoint_step is a
      list of ints, replace each int with its corresponding path (if
      find_closest, the path with the closest global step). If
      checkpoint_step == "all", return the path of every checkpoint in
      model_dir, starting from the earliest checkpoint. If
      checkpoint_step == -1, return the latest checkpoint as specified in
      model_dir/checkpoint. If checkpoint_step is None, return
      `tf.train.checkpoints_iterator` for `model_dir`.
    model_dir: str, directory to look for checkpoints in.
    skip_until: an integer - for "all" or "None" behavior, filter out
      checkpoint numbers that are <= skip_until.
    stop_after: an optional integer - for "None behavior, if specified
      stop after finding a checkpoint number that is >= stop_at. When a
      checkpoint number == stop_at is found, it is yielded before exiting.
    find_closest: If True and a specified checkpoint step does not exist, will
      choose the nearest checkpoint to that step. If False, then will
      only look for a checkpoint matching the exact specified step.

  Returns:
    An iterable which yields checkpoint paths.
  """

  def _get_closest_checkpoint(target_checkpoint):
    """Returns checkpoint with closest global step to `target_checkpoint`."""
    checkpoints = set()
    for f in tf.io.gfile.listdir(model_dir):
      try:
        checkpoints.add(int(get_step_from_checkpoint_path(f)))
      except ValueError:
        continue
    if not checkpoints:
      raise ValueError("No checkpoint files found in {}".format(model_dir))
    closest = float("inf")
    for c in checkpoints:
      if abs(target_checkpoint - c) < abs(target_checkpoint - closest):
        closest = c
    if closest != target_checkpoint:
      tf.logging.info(
          "Using checkpoint at step %d which is closest to requested step %d",
          closest,
          target_checkpoint,
      )
    return closest

  def _get_checkpoint_path(step):
    return os.path.join(model_dir, "model.ckpt-{}".format(step))

  def _get_checkpoint_path_if_exists(step):
    path = _get_checkpoint_path(step)
    return path if tf.train.checkpoint_exists(path) else None

  def _filter_fn(p):
    return get_step_from_checkpoint_path(p) > skip_until

  if checkpoint_step == "all":
    ckpt_paths = tf.gfile.Glob(os.path.join(model_dir, "model.ckpt*"))
    # Use set for deduplication; glob will find multiple files for each ckpt
    ckpt_steps = {get_step_from_checkpoint_path(p) for p in ckpt_paths}
    return filter(_filter_fn,
                  [_get_checkpoint_path(s) for s in sorted(list(ckpt_steps))])
  elif checkpoint_step == -1:
    return [tf.train.latest_checkpoint(model_dir)]
  elif checkpoint_step is None:
    checkpoints_iterator = filter(
        _filter_fn, tf.train.checkpoints_iterator(model_dir))
    if stop_after is not None:
      def _generate_checkpoints():
        for p in checkpoints_iterator:
          step = get_step_from_checkpoint_path(p)
          if step <= stop_after:
            yield p
          if step >= stop_after:
            break
      return _generate_checkpoints()
    else:
      return checkpoints_iterator
  elif find_closest:
    if isinstance(checkpoint_step, int):
      return [_get_checkpoint_path(_get_closest_checkpoint(checkpoint_step))]
    else:
      closests = np.unique(
          [_get_closest_checkpoint(c) for c in checkpoint_step])
      return [_get_checkpoint_path(closest) for closest in closests]
  else:
    if isinstance(checkpoint_step, int):
      checkpoint_step = [checkpoint_step]
    checkpoints = [_get_checkpoint_path_if_exists(c) for c in checkpoint_step]
    checkpoints = [c for c in checkpoints if c]
    if not checkpoints:
      raise ValueError("You asked for checkpoints '%s' but none were found." %
                       str(checkpoint_step))
    return checkpoints


# TODO(noam): provide a more informative string for layout_rules:
# example: "d_ff:model,heads:model,vocab:model"
@gin.configurable
def run(tpu_job_name,
        tpu,
        gcp_project,
        tpu_zone,
        model_dir,
        model_type="bitransformer",
        vocabulary=None,
        train_dataset_fn=None,
        eval_dataset_fn=None,
        dataset_split="train",
        autostack=True,
        eval_checkpoint_step=None,
        export_checkpoint_step=None,
        export_path="",
        mode="train",
        iterations_per_loop=100,
        save_checkpoints_steps=5000,
        keep_checkpoint_max=None,
        eval_summary_dir=None,
        batch_size=("tokens_per_replica", 2048),
        train_steps=auto_train_steps,
        total_run_steps=None,
        sequence_length=None,
        mesh_shape=gin.REQUIRED,
        mesh_devices=None,
        layout_rules=gin.REQUIRED,
        learning_rate_schedule=None,
        optimizer=None,
        predict_fn=None,
        variable_filter=None,
        perplexity_eval_steps=100,
        init_checkpoint=None,
        ensemble_inputs=None,
        train_model_fn=train_model,
        skip_seen_data=False,
        seen_data_init_step=0,
        output_eval_examples=True,
        checkpoint_input_pipeline=False,
        eval_dir_suffix=None):
  """Run training, eval, or inference depending on `mode`.

  Args:
    tpu_job_name: string, name of TPU worker binary
    tpu: string, the Cloud TPU to use for training
    gcp_project: string, project name for the Cloud TPU-enabled project
    tpu_zone: string, GCE zone where the Cloud TPU is located in
    model_dir: string, estimator model_dir
    model_type: a string, see `get_estimator` docstring for details.
    vocabulary: a vocabulary.Vocabulary or (inputs_vocabulary,
      targets_vocabulary) tuple.
    train_dataset_fn: A function returning a tf.data.Dataset, see `train_model`
      docstring for details.
    eval_dataset_fn: A function returning a list of dataset.EvalDataset tuples.
      See `eval_model` docstring for details.
    dataset_split: a string
    autostack: boolean, see `get_estimator` docstring for details.
    eval_checkpoint_step: int, list of ints, or None, see `eval_model` doc
      string for details.
    export_checkpoint_step: int or None, see `export_model` doc string for
      details.
    export_path: a string, path to export the saved model
    mode: string, one of
      train - train the model
      eval - eval the model by decoding predictions
      score_eval - eval the model by computing log likelihood scores of targets
      perplexity_eval - eval the model by computing perplexity
      infer - decode predictions based on inputs
      score_from_dataset - compute scores of targets from a dataset
      score_from_strings - compute scores of targets from strings or a file
      export_score - export a model that scores provided examples
      export_infer - export a model that decodes predictions based on inputs
    iterations_per_loop: integer, steps per train loop
    save_checkpoints_steps: integer, see `get_estimator` docstring.
    keep_checkpoint_max: an integer, see `get_estimator` docstring.
    eval_summary_dir: str, see `eval_model` docstring for details.
    batch_size: An integer or a (method, value) pair to pass to
      compute_batch_size(). Note that this is the global batch size and not the
      per-shard batch size.
    train_steps: An integer or a function with the same signature as
      auto_train_steps().  Total number of training steps in this run.
    total_run_steps: An integer, used when training is split over multiple
      runs. This value is gin-configurable and used to set the total_run_steps
      for the learning_rate_schedule.
    sequence_length: an integer or a dict from feature-key to integer
      the (packed) sequence length, e.g. {"inputs": 512, "targets": 128}.
      May also be set to `None` in eval mode to automatically compute the
      maximum length of the examples.
    mesh_shape: an input to mtf.convert_to_shape()
    mesh_devices: a list of strings, see `get_estimator` docstring.
    layout_rules: an input to mtf.convert_to_layout_rules()
    learning_rate_schedule: a function which takes the scalar name argument
      `step` and the numeric argument `total_train_steps` and returns the scalar
      learning rate.  Alternatively a float.  Alternatively, a list of
      such factos to be multiplied together.
    optimizer: a class extending optimize.Optimizer, required for training
    predict_fn: an optional function, see `get_estimator` docstring for details.
    variable_filter: a string, see `get_estimator` docstring for details.
    perplexity_eval_steps: an integer - number of steps for perplexity eval
    init_checkpoint: a string, see `get_estimator` docstring for details.
    ensemble_inputs: an integer, see `train_model` docstring for details.
    train_model_fn: an optional train function, is `train_model` by default.
    skip_seen_data: a boolean, is `False` by default. Used when a training run
      restarts to skip already seen data. This flag is only consistent when
      every setting (such as batch size and random seed) on the model is the
      same between the original run and the new run. May require a significant
      amount of time to skip a large number of steps.
    seen_data_init_step: an integer, when `skip_seen_data` is True, skip seen
      steps from this starting point. Useful when finetuning.
    output_eval_examples: a boolean, is `True` by default. Used to decide
      whether to output whether to dump inputs, targets, and predictions of the
      eval examples in plaintext to eval_summary_dir.
    checkpoint_input_pipeline: a boolean, whether to checkpoint the input
      pipeline in order to restart from the previous run. May require a large
      amount of disk space for complicated input pipelines.
    eval_dir_suffix: a string, if not None then will be appended to the eval
      subdirectory name for all three eval modes:
      `perplexity_eval`, `eval`, `score_eval`.
  """
  if isinstance(sequence_length, int):
    sequence_length = {"inputs": sequence_length,
                       "targets": sequence_length}

  if not isinstance(batch_size, int):
    batch_size = compute_batch_size(
        sequence_length, mesh_shape, layout_rules, batch_size)

  if not isinstance(train_steps, int):
    train_steps = train_steps(batch_size, sequence_length)

  if total_run_steps is None:
    total_run_steps = train_steps
  if isinstance(learning_rate_schedule, list):
    learning_rate_schedule = functools.partial(
        learning_rate_schedules.product_learning_rate,
        total_train_steps=total_run_steps, factors=learning_rate_schedule)

  if callable(learning_rate_schedule):
    learning_rate_schedule = functools.partial(
        learning_rate_schedule, total_train_steps=total_run_steps)

  tf.logging.info("model_type=%s", model_type,)
  tf.logging.info("mode=%s", mode,)
  tf.logging.info("sequence_length=%s", sequence_length,)
  tf.logging.info("batch_size=%s", batch_size,)
  tf.logging.info("train_steps=%s", train_steps,)
  if total_run_steps is not None:
    tf.logging.info("total_run_steps=%s", total_run_steps,)
  tf.logging.info("mesh_shape=%s", mesh_shape,)
  tf.logging.info("layout_rules=%s", layout_rules,)

  if mode == "train" and dataset_split != "train":
    raise ValueError("mode==\"train\" requires dataset_split==\"train\"")

  if mode != "train":
    ensemble_inputs = None

  mesh_shape = mtf.convert_to_shape(mesh_shape)
  layout_rules = mtf.convert_to_layout_rules(layout_rules)

  cluster = tf.distribute.cluster_resolver.TPUClusterResolver(
      tpu, zone=tpu_zone, project=gcp_project) if tpu else None

  tf.logging.info("Building TPUConfig with tpu_job_name=%s", tpu_job_name)

  score_in_predict_mode = "score" in mode
  estimator_fn = functools.partial(
      get_estimator,
      model_type=model_type,
      vocabulary=vocabulary,
      layout_rules=layout_rules,
      mesh_shape=mesh_shape,
      model_dir=model_dir,
      batch_size=batch_size,
      sequence_length=sequence_length,
      autostack=autostack,
      learning_rate_schedule=learning_rate_schedule,
      keep_checkpoint_max=keep_checkpoint_max,
      save_checkpoints_steps=save_checkpoints_steps,
      optimizer=optimizer,
      predict_fn=predict_fn,
      score_in_predict_mode=score_in_predict_mode,
      variable_filter=variable_filter,
      init_checkpoint=init_checkpoint,
      ensemble_inputs=ensemble_inputs,
      use_tpu=tpu,
      tpu_job_name=tpu_job_name,
      iterations_per_loop=iterations_per_loop,
      cluster=cluster,
      mesh_devices=mesh_devices)

  if mode not in ("eval", "score_eval"):
    if sequence_length is None:
      raise ValueError(f"`sequence_length` must be specified in '{mode}' mode.")
    estimator = estimator_fn()

  if mode == "train":
    # train_dataset_fn could be None if train_model_fn is not equal to
    # train_model
    if train_dataset_fn is None:
      raise ValueError("Must provide train_dataset_fn through gin")

    train_model_fn(estimator, vocabulary, sequence_length, batch_size,
                   train_dataset_fn, train_steps, ensemble_inputs,
                   skip_seen_data=skip_seen_data,
                   seen_data_init_step=seen_data_init_step,
                   checkpoint_input_pipeline=checkpoint_input_pipeline)

  elif mode == "perplexity_eval":
    if eval_dataset_fn is None:
      if train_dataset_fn is not None:
        tf.logging.warning("Using train_dataset_fn for perplexity eval")
        eval_datasets = [transformer_dataset.EvalDataset(
            name="eval",
            dataset_fn=functools.partial(train_dataset_fn,
                                         sequence_length=sequence_length,
                                         vocabulary=vocabulary,
                                         dataset_split=dataset_split),
            postprocess_fn=None,
            metric_fns=None)]
      else:
        raise ValueError(
            "for perplexity_eval, "
            "must provide one of eval_dataset_fn and train_dataset_fn")
    else:
      eval_datasets = eval_dataset_fn(
          sequence_length=sequence_length,
          vocabulary=vocabulary,
          dataset_split=dataset_split,
      )
    def _input_fn(params, eval_dataset):
      del params
      ds = eval_dataset.dataset_fn().map(
          _filter_features, num_parallel_calls=tf.data.experimental.AUTOTUNE)
      ds = transformer_dataset.pad_dataset_with_zeroed_out_examples(ds)
      ds = (ds.batch(batch_size * (ensemble_inputs or 1), drop_remainder=True)
            .prefetch(tf.data.experimental.AUTOTUNE))
      return ds
    checkpoint_paths = get_checkpoint_iterator(eval_checkpoint_step, model_dir)
    for checkpoint_path in checkpoint_paths:
      for eval_dataset in eval_datasets:
        tf.random.set_random_seed(12345)
        random.seed(12345)
        num_examples = batch_size * perplexity_eval_steps
        # include the number of examples in the evaluation name so as to
        # make sure we are comparing apples to apples.
        name = "%s_%s_%d" % (eval_dataset.name, dataset_split, num_examples)
        if eval_dir_suffix is not None:
          name += "_%s" % eval_dir_suffix
        _ = estimator.evaluate(
            input_fn=functools.partial(_input_fn, eval_dataset=eval_dataset),
            steps=perplexity_eval_steps,
            checkpoint_path=checkpoint_path,
            name=name)
  elif mode in ("eval", "score_eval"):
    eval_model(
        estimator_fn,
        vocabulary,
        sequence_length,
        batch_size,
        dataset_split,
        model_dir,
        eval_dataset_fn,
        eval_summary_dir,
        eval_checkpoint_step,
        eval_with_score=(mode == "score_eval"),
        output_eval_examples=output_eval_examples,
        eval_dir_suffix=eval_dir_suffix)
  elif mode == "infer":
    infer_model(estimator, vocabulary, sequence_length, batch_size, model_type,
                model_dir, eval_checkpoint_step)
  elif mode == "score_from_strings":
    score_from_strings(estimator=estimator,
                       vocabulary=vocabulary,
                       model_type=model_type,
                       batch_size=batch_size,
                       sequence_length=sequence_length,
                       model_dir=model_dir,
                       eval_checkpoint_step=eval_checkpoint_step)
  elif mode == "score_from_dataset":
    score_from_dataset(estimator, vocabulary, batch_size, sequence_length,
                       model_dir, eval_checkpoint_step, dataset_split)
  elif mode in ["export_score", "export_infer", "export"]:
    if mode == "export":
      tf.logging.warning("Mode 'export' is deprecated. "
                         "Defaulting to 'export_infer'.")
    if export_checkpoint_step:
      checkpoint_path = get_checkpoint_iterator(
          export_checkpoint_step, model_dir)
      if isinstance(checkpoint_path, list):
        checkpoint_path = checkpoint_path[0]
      else:
        checkpoint_path = next(checkpoint_path)
    else:
      # Use the latest checkpoint in the model directory.
      checkpoint_path = None
    export_model(estimator, export_path, vocabulary, sequence_length,
                 model_type, score_in_predict_mode, batch_size, checkpoint_path)

  else:
    raise ValueError(
        "unknown mode %s - must be train/perplexity_eval/eval/infer/export"
        % mode)