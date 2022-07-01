#  Copyright 2022, Lefebvre Dalloz Services
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

"""
All the tooling to ease ONNX Runtime usage.
"""
import copy
import logging
import multiprocessing
from collections import defaultdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union

import numpy as np
import onnx
import torch
from onnx import ModelProto, NodeProto
from onnx.shape_inference import infer_shapes_path
from onnxruntime import ExecutionMode, GraphOptimizationLevel, InferenceSession, IOBinding, OrtValue, SessionOptions
from onnxruntime.quantization import QuantType, quantize_dynamic
from onnxruntime.transformers import optimizer
from onnxruntime.transformers.float16 import convert_float_to_float16
from onnxruntime.transformers.fusion_options import FusionOptions
from onnxruntime.transformers.fusion_utils import FusionUtils
from onnxruntime.transformers.onnx_model import OnnxModel
from onnxruntime.transformers.onnx_model_bert import BertOnnxModel
from onnxruntime.transformers.optimizer import MODEL_TYPES


# GPU inference only
try:
    # noinspection PyUnresolvedReferences
    import cupy as cp
except ImportError:
    pass


def create_model_for_provider(
    path: str,
    provider_to_use: Union[str, List],
    nb_threads: int = multiprocessing.cpu_count(),
    nb_instances: int = 0,
    optimization_level: GraphOptimizationLevel = GraphOptimizationLevel.ORT_ENABLE_EXTENDED,
    enable_profiling: bool = False,
    log_severity: int = 2,
) -> InferenceSession:
    """
    Create an ONNX Runtime instance.
    :param path: path to ONNX file or serialized to string model
    :param provider_to_use: provider to use for inference
    :param nb_threads: intra_op_num_threads to use. You may want to try different parameters,
        more core does not always provide best performances.
    :param nb_instances: inter_op_num_threads to use, to execute multiple subgraphs in parallel when possible.
    :param optimization_level: expected level of ONNX Runtime optimization. For GPU and NLP, extended is the one
        providing kernel fusion of element wise operations. Enable all level is for CPU inference.
        see https://onnxruntime.ai/docs/performance/graph-optimizations.html#layout-optimizations
    :param enable_profiling: let Onnx Runtime log each kernel time.
    :param log_severity: Log severity level. 0:Verbose, 1:Info, 2:Warning. 3:Error, 4:Fatal.
    :return: ONNX Runtime inference session
    """
    options = SessionOptions()
    options.graph_optimization_level = optimization_level
    options.enable_profiling = enable_profiling
    options.log_severity_level = log_severity
    if isinstance(provider_to_use, str):
        provider_to_use = [provider_to_use]
    if provider_to_use == ["CPUExecutionProvider"]:
        options.execution_mode = ExecutionMode.ORT_SEQUENTIAL if nb_instances <= 1 else ExecutionMode.ORT_PARALLEL
        options.intra_op_num_threads = nb_threads
        if nb_instances > 1:
            options.inter_op_num_threads = nb_instances
    return InferenceSession(path, options, providers=provider_to_use)


def optimize_onnx(
    onnx_path: str,
    onnx_optim_model_path: str,
    fp16: bool,
    use_cuda: bool,
    num_attention_heads: int = 0,
    hidden_size: int = 0,
    architecture: str = "bert",
) -> None:
    """
    ONNX Runtime transformer graph optimization.
    Performs some operator fusion (merge several nodes of the graph in a single one)
    and may convert some nodes to reduced precision.
    :param onnx_path: ONNX input path
    :param onnx_optim_model_path: where to save optimized model
    :param fp16: use mixed precision (faster inference)
    :param use_cuda: perform optimization on GPU (should )
    :param num_attention_heads: number of attention heads of a model (0 -> try to detect)
    :param hidden_size: hidden layer size of a model (0 -> try to detect)
    :param architecture: model architecture to optimize. One of [bert, bart, gpt2]
    """
    optimization_options = FusionOptions(model_type=architecture)
    optimization_options.enable_gelu_approximation = False  # additional optimization
    if architecture == "distilbert":
        optimization_options.enable_embed_layer_norm = False
    if architecture not in MODEL_TYPES:
        logging.info(f"Unknown architecture {architecture} for Onnx Runtime optimizer, overriding with 'bert' value")
        architecture = "bert"
    opt_level = 1 if architecture == "bert" else 0
    optimized_model: BertOnnxModel = optimizer.optimize_model(
        input=onnx_path,
        model_type=architecture,
        use_gpu=use_cuda,
        opt_level=opt_level,
        num_heads=num_attention_heads,  # automatic detection with 0 may not work with opset 13 or distilbert models
        hidden_size=hidden_size,  # automatic detection with 0
        optimization_options=optimization_options,
    )
    if fp16:
        # use_symbolic_shape_infer set to false because doesn't work after ONNX package v1.10.2
        optimized_model.convert_float_to_float16(use_symbolic_shape_infer=False)  # FP32 -> FP16
    logging.info(f"optimizations applied: {optimized_model.get_fused_operator_statistics()}")
    optimized_model.save_model_to_file(onnx_optim_model_path)


def cpu_quantization(input_model_path: str, output_model_path: str) -> None:
    """
    ONNX CPU only dynamic quantization.

    :param input_model_path: ONNX graph (float) to quantize
    :param output_model_path: where to save quantized model
    """
    quantize_dynamic(
        model_input=Path(input_model_path),
        model_output=Path(output_model_path),
        op_types_to_quantize=["MatMul", "Attention"],
        weight_type=QuantType.QInt8,
        per_channel=True,
        reduce_range=True,
        extra_options={"WeightSymmetric": False, "MatMulConstBOnly": True},
    )


# https://github.com/pytorch/pytorch/blob/ac79c874cefee2f8bc1605eed9a924d80c0b3542/torch/testing/_internal/common_utils.py#L349
numpy_to_torch_dtype_dict = {
    bool: torch.bool,
    np.uint8: torch.uint8,
    np.int8: torch.int8,
    np.int16: torch.int16,
    np.int32: torch.int32,
    np.int64: torch.int64,
    np.float16: torch.float16,
    np.float32: torch.float32,
    np.float64: torch.float64,
    np.complex64: torch.complex64,
    np.complex128: torch.complex128,
}

torch_to_numpy_dtype_dict = {v: k for k, v in numpy_to_torch_dtype_dict.items()}

ort_to_numpy_dtype_dict = {
    "tensor(bool)": np.uint8,  # bool not supported by DlPack! https://github.com/dmlc/dlpack/issues/75
    "tensor(float16)": np.float16,
    "tensor(float)": np.float32,
    "tensor(float64)": np.float64,
    "tensor(int32)": np.int32,
    "tensor(int64)": np.int64,
}


# TODO add test including different input and checking that tensor is not overriden
def to_pytorch(ort_tensor: OrtValue, clone_tensor: bool) -> torch.Tensor:
    """
    Convert OrtValue output by Onnx Runtime to Pytorch tensor.
    The process can be done in a zero copy way (depending of clone parameter).
    :param ort_tensor: output from Onnx Runtime
    :param clone_tensor: Onnx Runtime owns the storage array and will write on the next inference.
        By cloning you guarantee that the data won't change.
    :return: Pytorch tensor
    """
    if ort_tensor.device_name().lower() == "cuda":
        np_type = ort_to_numpy_dtype_dict[ort_tensor.data_type()]
        fake_owner = 1
        # size not used anywhere, so just put 0
        memory = cp.cuda.UnownedMemory(ort_tensor.data_ptr(), 0, fake_owner)
        memory_ptr = cp.cuda.MemoryPointer(memory, 0)
        # make sure you interpret the array shape/dtype/strides correctly
        cp_array = cp.ndarray(shape=ort_tensor.shape(), memptr=memory_ptr, dtype=np_type)
        # cloning required otherwise ORT will recycle the storage array and put new values into it if new inf is done.
        torch_tensor = torch.from_dlpack(cp_array.toDlpack())
        if clone_tensor:
            torch_tensor = torch_tensor.clone()
        return torch_tensor
    else:
        np_tensor = ort_tensor.numpy()
        return torch.from_numpy(np_tensor)


def inference_onnx_binding(
    model_onnx: InferenceSession,
    inputs: Dict[str, torch.Tensor],
    device: str,
    device_id: int = 0,
    binding: Optional[IOBinding] = None,
    clone_tensor: bool = True,
) -> Dict[str, torch.Tensor]:
    """
    Performs inference on ONNX Runtime in an optimized way.
    In particular, it avoids any Onnx Runtime output tensor copy.
    It means that Onnx Runtime is still owner of the array, and it will overwrite its content if you do another
    inference. To avoid any issue, just set clone_tensor to True (default).
    For best performance and lowest memory footprint, if you know what you are doing, set clone_tensor to False.

    :param model_onnx: ONNX model
    :param inputs: input torch tensor
    :param device: where to run the inference. One of [cpu, cuda]
    :param device_id: ID of the device where to run the inference, to be used when there are multiple GPUs, etc.
    :param binding: previously generated binding IO, will be reset.
    :param clone_tensor: clone Pytorch tensor to avoid its content being overwritten by Onnx Runtime
        at the next inference call.
    :return: a dict {axis name: output tensor}
    """
    assert isinstance(device, str)
    assert device in ["cpu", "cuda"], f"unexpected inference device: '{device}'"
    if binding is None:
        binding: IOBinding = model_onnx.io_binding()
    else:
        binding.clear_binding_inputs()
        binding.clear_binding_outputs()
    for input_onnx in model_onnx.get_inputs():
        if input_onnx.name not in inputs:  # some inputs may be optional
            continue
        tensor: torch.Tensor = inputs[input_onnx.name]
        tensor = tensor.detach()
        if tensor.dtype in [torch.int64, torch.long]:
            # int32 mandatory as input of bindings, int64 not supported
            tensor = tensor.type(dtype=torch.int32)
        tensor = tensor.contiguous()
        binding.bind_input(
            name=input_onnx.name,
            device_type=device,
            device_id=device_id,
            element_type=torch_to_numpy_dtype_dict[tensor.dtype],
            shape=tuple(tensor.shape),
            buffer_ptr=tensor.data_ptr(),
        )
        inputs[input_onnx.name] = tensor

    for out in model_onnx.get_outputs():
        binding.bind_output(
            name=out.name,
            device_type=device,
            device_id=device_id,
        )
    binding.synchronize_inputs()
    model_onnx.run_with_iobinding(binding)
    binding.synchronize_outputs()
    outputs = dict()
    assert len(model_onnx.get_outputs()) == len(
        binding.get_outputs()
    ), f"{len(model_onnx.get_outputs())} != {len(binding.get_outputs())}"
    for out, t in zip(model_onnx.get_outputs(), binding.get_outputs()):
        outputs[out.name] = to_pytorch(t, clone_tensor=clone_tensor)
    return outputs


def add_output_nodes(model: ModelProto) -> ModelProto:
    """
    Set each node as output node for debugging purpose.
    :param model: ONNX model in protobuf format
    :return: modified ONNX model
    """
    model = copy.deepcopy(model)
    output_nodes = list()
    for n in model.graph.node:
        for output_name in n.output:
            output_nodes.append(onnx.ValueInfoProto(name=output_name))
    # clear output array (protobuff way...)
    while model.graph.output:
        model.graph.output.pop()
    model.graph.output.extend(output_nodes)
    return model


def find_node_fp32(graph: Dict[str, str], output_nodes: Dict[str, torch.Tensor]) -> List[str]:
    """
    Identify out of range values in node outputs.
    :param graph: graph as adjency nodes dict
    :param output_nodes: output of each node
    :return: list of nodes producing outputs outside fp16 tensor
    """
    keep_fp32 = list()
    min_float16 = torch.finfo(torch.float16).min
    max_float16 = torch.finfo(torch.float16).max
    resolution = 5.96e-08  # torch.finfo(torch.float16).eps  # minimum value that can be represented by FP16
    for k, tensor in output_nodes.items():
        if tensor.dtype != torch.float32:
            continue
        # out of FP16 range
        if (
            torch.any(tensor > max_float16)
            or torch.any(tensor < min_float16)
            or (torch.any((tensor < resolution) & (tensor > -resolution) & (tensor != 0)))  # limited memory footprint
        ):
            keep_fp32.append(graph[k])
    return keep_fp32


def get_io_to_node_mapping(onnx_model: ModelProto) -> Tuple[Dict[str, str], Dict[str, str]]:
    """
    Extract output->node and input->node mappings
    :param onnx_model: ONNX model
    :return: 2 mappings, (i->node, o->node)
    """
    output_mapping: Dict[str, str] = dict()
    input_mapping: Dict[str, str] = dict()
    for node in onnx_model.graph.node:  # type: NodeProto
        assert len(node.output) == 1
        output_node = node.output[0]
        output_mapping[output_node] = node.name
        for i in node.input:
            input_mapping[i] = node.name

    return input_mapping, output_mapping


def use_external_data(path: str) -> bool:
    """
    Check if a model uses external data
    :param path: Onnx model path
    :return: True if any initalizer (model weight) is stored in an external file
    """
    model = onnx.load_model(f=path, load_external_data=False)
    for i in model.graph.initializer:
        if i.HasField("data_location") and i.data_location == onnx.TensorProto.EXTERNAL:
            return True
    return False


def get_keep_fp32_nodes(
    onnx_model_path: str,
    get_input: Callable[[], Dict[str, torch.Tensor]],
    early_stop: int = 100,
    device: str = "cuda",
) -> List[str]:
    """
    Find the list of nodes to keep in FP32 to avoid out of range values
    :param onnx_model_path: ONNX model path
    :param get_input: generate input to test the model. Output should change from call to call
    :param early_stop: will test until `early_stop` tests are done without any new node to keep in FP32
    :param device: where to run the inference
    :return: list of names of nodes to keep in FP32
    """
    # do not load weights on LLM (>2Gb), we only need to modify the computation graph
    onnx_model: ModelProto = onnx.load_model(f=onnx_model_path, load_external_data=False)
    onnx_model_fp32_all_nodes = add_output_nodes(model=onnx_model)
    path_onnx_model_fp32_all_nodes = onnx_model_path + "_all_nodes.onnx"
    onnx.save_model(proto=onnx_model_fp32_all_nodes, f=path_onnx_model_fp32_all_nodes, save_as_external_data=False)
    provider = "CUDAExecutionProvider" if device == "cuda" else "CPUExecutionProvider"
    ort_model_fp32_all_nodes = create_model_for_provider(path_onnx_model_fp32_all_nodes, provider)
    ort_binding = ort_model_fp32_all_nodes.io_binding()
    input_mapping, output_mapping = get_io_to_node_mapping(onnx_model=onnx_model)
    # list all nodes which have an output out of the FP16 range
    keep_fp32_nodes = list()
    no_new_node_counter = 0
    while no_new_node_counter < early_stop:
        inputs = get_input()
        outputs: Dict[str, torch.Tensor] = inference_onnx_binding(
            model_onnx=ort_model_fp32_all_nodes, inputs=inputs, device=device, binding=ort_binding, clone_tensor=False
        )
        keep_node_io = find_node_fp32(graph=output_mapping, output_nodes=outputs)

        nodes_to_add = [n for n in keep_node_io if n not in keep_fp32_nodes]
        keep_fp32_nodes += nodes_to_add
        if len(nodes_to_add) == 0:
            no_new_node_counter += 1
        else:
            no_new_node_counter = 0

    if device == "cuda":
        torch.cuda.empty_cache()
    # I/O names that can't be found in the graph
    nodes_to_skip = (
        [n.name for n in onnx_model.graph.input]
        + [n.name for n in onnx_model.graph.output]
        + [n.name for n in onnx_model.graph.initializer]
    )

    # for each node to keep in FP32, we keep its children in FP32 too as they will receive FP32 values as input
    map_children = defaultdict(list)
    for node in onnx_model.graph.node:
        for o in node.output:
            if o in nodes_to_skip:
                continue
            child = input_mapping[o]
            map_children[node.name].append(child)
    keep_fp32_nodes += [c for k in keep_fp32_nodes if k in map_children for c in map_children[k]]
    return keep_fp32_nodes


def convert_fp16(onnx_model: str, nodes_to_exclude: List[str]) -> ModelProto:
    """
    Convert ONNX model in FP16, and still being able to exclude a list of nodes.
    :param onnx_model: original FP32 model
    :param nodes_to_exclude: nodes that should stay in FP32
    :return: mostly FP16 model
    """
    # add value info related to each node, required for the conversion
    output_path = onnx_model + "_shape_inference.onnx"
    infer_shapes_path(model_path=onnx_model, output_path=output_path)
    model_fp16 = onnx.load_model(output_path)
    model_fp16 = convert_float_to_float16(model=model_fp16, keep_io_types=False, node_block_list=nodes_to_exclude)
    # clean casting nodes before returning the model
    wrapped_fp16_model = OnnxModel(model_fp16)
    fusion_utils = FusionUtils(wrapped_fp16_model)
    fusion_utils.remove_cascaded_cast_nodes()
    fusion_utils.remove_useless_cast_nodes()
    wrapped_fp16_model.topological_sort()
    return wrapped_fp16_model.model
