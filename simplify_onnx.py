from pathlib import Path
from typing import Dict, List

import numpy as np
import onnx
import onnxruntime
import onnxsim
import streamlit as st
from easydict import EasyDict


def infer_shapes(
    model: onnx.ModelProto, ops: List[str] = tuple(), names: List[str] = tuple()
) -> Dict[str, List[int]]:
    """
    推断ONNX模型中指定类型算子的输出维度

    Args:
        model (onnx.ModelProto): ONNX模型
        ops (List[str], optional): 需要推断的算子类型. Defaults to None.
        names (List[str], optional): 需要推断的节点名称. Defaults to None.

    Returns:
        Dict[str, List[int]]: 节点名称到输出维度的映射
    """
    model = onnx.load_model_from_string(model.SerializeToString())
    output_names = []
    for node in model.graph.node:
        if node.op_type in ops or node.name in names:
            if node.name not in model.graph.output:
                model.graph.output.extend(
                    [
                        onnx.helper.make_tensor_value_info(
                            node.output[0], onnx.TensorProto.FLOAT, [None]
                        )
                    ]
                )
            output_names.append(node.output[0])

    inputs = {
        input_node.name: np.random.random_sample(
            [dim.dim_value for dim in input_node.type.tensor_type.shape.dim]
        ).astype(np.float32)
        for input_node in model.graph.input
    }
    sess = onnxruntime.InferenceSession(
        model.SerializeToString(),
        providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
    )
    outputs = sess.run(output_names, inputs)
    node_to_shape = {
        output_name: list(output.shape) for output, output_name in zip(outputs, output_names)
    }
    return node_to_shape


def replace_input_name(model: onnx.ModelProto) -> onnx.ModelProto:
    """将ONNX模型中的第一个输入名称替换为input

    Args:
        model (onnx.ModelProto): 需要进行操作的ONNX模型

    Returns:
        onnx.ModelProto: 替换后的ONNX模型
    """
    model = onnx.load_model_from_string(model.SerializeToString())

    old_name = model.graph.input[0].name
    model.graph.input[0].name = "input"
    # 遍历所有的节点，将输入名称为old_name的节点的输入名称替换为input
    for node in model.graph.node:
        for i in range(len(node.input)):
            if node.input[i] == old_name:
                node.input[i] = "input"

    return model


def replace_output_name(model: onnx.ModelProto) -> onnx.ModelProto:
    """将ONNX模型中的第一个输出名称替换为output

    Args:
        model (onnx.ModelProto): 需要进行操作的ONNX模型

    Returns:
        onnx.ModelProto: 替换后的ONNX模型
    """
    model = onnx.load_model_from_string(model.SerializeToString())

    old_name = model.graph.output[0].name
    model.graph.output[0].name = "output"
    # 遍历所有的节点，将输出名称为old_name的节点的输出名称替换为output
    for node in model.graph.node:
        for i in range(len(node.output)):
            if node.output[i] == old_name:
                node.output[i] = "output"

    return model


def simplify(model: onnx.ModelProto) -> onnx.ModelProto:
    """
    简化ONNX模型，去除无用的节点和参数

    Args:
        model (onnx.ModelProto): 需要简化的ONNX模型

    Raises:
        Exception: 简化ONNX模型失败

    Returns:
        onnx.ModelProto: 简化后的ONNX模型
    """
    model_sim, success = onnxsim.simplify(model)
    if not success:
        raise Exception("简化ONNX模型失败！")
    return model_sim


def fuse_conv_and_bn(model: onnx.ModelProto) -> onnx.ModelProto:
    """将ONNX模型中连续的Conv和BN节点合并为一个Conv节点

    Args:
        model (onnx.ModelProto): 需要重新编排的ONNX模型

    Returns:
        onnx.ModelProto: 重新编排后的ONNX模型
    """
    # 复制一份模型，防止修改原模型
    model = onnx.load_model_from_string(model.SerializeToString())

    # 寻找并处理连续的Conv和BN
    node_name_to_node = {}
    for node in model.graph.node:
        node_name_to_node[node.name] = node

    tensor_name_to_tensor = {}
    for tensor in model.graph.initializer:
        tensor_name_to_tensor[tensor.name] = onnx.numpy_helper.to_array(tensor)

    new_nodes = []
    for node in model.graph.node:
        # 如果节点是BN并且前面的节点是Conv
        if (
            node.op_type == "BatchNormalization"
            and node_name_to_node[node.input[0]].op_type == "Conv"
        ):
            # 取出Conv和BN的参数
            conv_node = node_name_to_node[node.input[0]]
            bn_node = node

            conv_weight = tensor_name_to_tensor[conv_node.input[1]]
            if len(conv_node.input) == 3:
                conv_bias = tensor_name_to_tensor[conv_node.input[2]]
            else:
                conv_bias = np.zeros(conv_weight.shape[0], dtype=np.float32)

            bn_scale = tensor_name_to_tensor[bn_node.input[1]]
            bn_bias = tensor_name_to_tensor[bn_node.input[2]]
            bn_mean = tensor_name_to_tensor[bn_node.input[3]]
            bn_var = tensor_name_to_tensor[bn_node.input[4]]
            eps = bn_node.attribute[0].f

            # 计算新的Conv的参数
            std = np.sqrt(bn_var + eps)
            t = bn_scale / std
            new_weight = conv_weight * t.reshape((-1, 1, 1, 1))
            new_bias = bn_scale * (conv_bias - bn_mean) / std + bn_bias

            # 更新Conv的参数
            conv_node.input[1] = bn_node.name + "_new_weight"
            tensor_name_to_tensor[conv_node.input[1]] = new_weight
            model.graph.initializer.append(
                onnx.numpy_helper.from_array(new_weight, bn_node.name + "_new_weight")
            )
            if len(conv_node.input) == 3:
                tensor_name_to_tensor[conv_node.input[2]] = new_bias
            else:
                conv_node.input.append(bn_node.name + "_new_bias")
                tensor_name_to_tensor[bn_node.name + "_new_bias"] = new_bias
            model.graph.initializer.append(
                onnx.numpy_helper.from_array(new_bias, bn_node.name + "_new_bias")
            )

            conv_node.output[0] = bn_node.output[0]
            new_nodes.append(conv_node)
        else:
            if (
                node.op_type != "BatchNormalization"
                or node_name_to_node[node.input[0]].op_type != "Conv"
            ):
                new_nodes.append(node)

    # 创建一个新的图
    new_graph = onnx.helper.make_graph(
        new_nodes,
        model.graph.name,
        model.graph.input,
        model.graph.output,
        model.graph.initializer,
    )

    # 替换原始的图
    model.graph.CopyFrom(new_graph)

    return model


def modify_reshape(model: onnx.ModelProto) -> onnx.ModelProto:
    """
    重新编排ONNX模型中的Reshape节点，使得batch维度为-1，其它维度为固定值。

    Args:
        model (onnx.ModelProto): 需要重新编排Reshape节点的ONNX模型

    Returns:
        onnx.ModelProto: 重新编排后的ONNX模型
    """
    # 复制一份模型，防止修改原模型
    model = onnx.load_model_from_string(model.SerializeToString())

    reshape_dims = infer_shapes(model, ops=["Reshape"])

    to_replace_initializers = []

    # 遍历图中的所有节点
    for node in model.graph.node:
        # 如果节点是Reshape操作
        if node.op_type == "Reshape":
            # 获取形状的输入名称
            shape_input_name = node.input[1]
            # 在图中找到这个输入
            for initializer in model.graph.initializer:
                if initializer.name == shape_input_name:
                    # 获取形状的值
                    shape = onnx.numpy_helper.to_array(initializer)
                    # 计算新的形状
                    new_shape = np.array(
                        reshape_dims[node.output[0]], dtype=shape.dtype
                    )
                    new_shape[0] = -1
                    # 更新形状的值
                    new_tensor = onnx.numpy_helper.from_array(
                        new_shape, name=f"{node.name}_shape"
                    )
                    # 替换原始的初始化器
                    to_replace_initializers.append((initializer, new_tensor))
                    node.input[1] = new_tensor.name
    for initializer, new_tensor in to_replace_initializers:
        if initializer in model.graph.initializer:
            model.graph.initializer.remove(initializer)
        model.graph.initializer.append(new_tensor)
    return model


def resolve_reduce_mean_axis(model: onnx.ModelProto) -> onnx.ModelProto:
    """替换reduce_mean中的axis，把负数维度替换成实际维度

    Args:
        model (onnx.ModelProto): ONNX模型

    Returns:
        onnx.ModelProto: 替换维度后的ONNX模型
    """
    model = onnx.load_model_from_string(model.SerializeToString())

    reduce_mean_dims = infer_shapes(model, ops=["ReduceMean"])

    # 遍历所有的节点
    for node in model.graph.node:
        # 如果节点是ReduceMean操作
        if node.op_type != "ReduceMean":
            continue
        for attribute in node.attribute:
            if attribute.name == "axes":
                new_axes = []
                for axis in attribute.ints:
                    if axis < 0:
                        axis = len(reduce_mean_dims[node.input[0]]) + axis
                    new_axes.append(axis)
                attribute.ints[:] = new_axes
    return model


def replace_squeeze_and_unsqueeze(model: onnx.ModelProto) -> onnx.ModelProto:
    """用Reshape算子替换onnx中的squeeze和unsqueeze算子

    Args:
        model (onnx.ModelProto): 需要重新编排Reshape节点的ONNX模型

    Returns:
        onnx.ModelProto: 重新编排后的ONNX模型
    """
    # 复制一份模型，防止修改原模型
    model = onnx.load_model_from_string(model.SerializeToString())

    reshape_dims = infer_shapes(model, ops=["Squeeze", "Unsqueeze"])

    new_nodes = []
    # 遍历原始模型的所有节点
    for node in model.graph.node:
        # 如果节点是Squeeze或Unsqueeze操作
        if node.op_type in ["Squeeze", "Unsqueeze"]:
            new_shape = reshape_dims[node.output[0]]
            new_shape[0] = -1
            reshape_param = onnx.helper.make_tensor(
                name=f"{node.name}_reshape_param",
                data_type=onnx.TensorProto.INT64,
                dims=[len(new_shape)],
                vals=new_shape,
            )
            # 创建一个新的Reshape操作
            reshape_node = onnx.helper.make_node(
                "Reshape",
                inputs=[node.input[0], reshape_param.name],
                outputs=node.output,
                name=node.name,
            )
            # 将新的Reshape操作和shape参数添加到图中
            model.graph.initializer.append(reshape_param)

            # 将新的Reshape节点添加到新的模型图中
            new_nodes.append(reshape_node)
        else:
            # 如果节点不是Squeeze或Unsqueeze操作，直接添加到新的模型图中
            new_nodes.append(node)

    # 创建一个新的图
    new_graph = onnx.helper.make_graph(
        new_nodes,
        model.graph.name,
        model.graph.input,
        model.graph.output,
        model.graph.initializer,
    )

    # 替换原始的图
    model.graph.CopyFrom(new_graph)

    return model


def merge_slice(model: onnx.ModelProto) -> onnx.ModelProto:
    """将onnx模型中的Slice节点合并为Split节点

    Args:
        model (onnx.ModelProto): 需要进行操作的onnx模型

    Returns:
        onnx.ModelProto: 合并后的onnx模型
    """
    model = onnx.load_model_from_string(model.SerializeToString())

    # 获取模型中的initializer
    initializers = dict((init.name, init) for init in model.graph.initializer)

    # 创建一个列表来存储每个Slice节点的名称
    slice_dict = {}
    for node in model.graph.node:
        if node.op_type == "Slice":
            if node.input[0] not in slice_dict:
                slice_dict[node.input[0]] = []
            starts_node = initializers[node.input[1]]
            starts = onnx.numpy_helper.to_array(starts_node)[0]
            ends_node = initializers[node.input[2]]
            ends = onnx.numpy_helper.to_array(ends_node)[0]
            axis_node = initializers[node.input[3]]
            axis = onnx.numpy_helper.to_array(axis_node)[0]
            slice_dict[node.input[0]].append(
                dict(node=node, starts=starts, ends=ends, axis=axis)
            )
        elif node.op_type == "Split":
            pass

    output_dims = infer_shapes(model, names=list(slice_dict.keys()))

    # 判断哪些slice操作可以合并
    to_merge = {}
    for input_name, slice_nodes in slice_dict.items():
        # 按照slice的起始位置对slice_nodes进行排序
        slice_nodes.sort(key=lambda node: node["starts"])
        # 判断是否有多个slice操作
        if len(slice_nodes) == 1:
            continue
        # 判断slice的axis是否相同
        for i in range(1, len(slice_nodes)):
            if slice_nodes[i]["axis"] != slice_nodes[0]["axis"]:
                continue
        axis = slice_nodes[0]["axis"]
        # 判断slice的start和end是否连续
        for i in range(1, len(slice_nodes)):
            if slice_nodes[i]["starts"] != slice_nodes[i - 1]["ends"]:
                continue
        # 判断是否使用slices的输出合并是否是父节点的输出
        total_dim = 0
        for i in range(len(slice_nodes)):
            total_dim += slice_nodes[i]["ends"] - slice_nodes[i]["starts"]
        if output_dims[input_name][axis] != total_dim:
            continue
        for i in range(len(slice_nodes)):
            to_merge[slice_nodes[i]["node"].name] = slice_nodes

    # 创建一个集合来存储已经处理过的Slice节点的名称
    processed = set()

    new_nodes = []
    # 遍历图中的所有节点
    for node in model.graph.node:
        if node.name in processed:
            continue
        if node.op_type != "Slice" or node.name not in to_merge:
            new_nodes.append(node)
            continue
        brother = to_merge[node.name]
        axis = brother[0]["axis"]
        splits = []
        for i in range(len(brother)):
            splits.append(brother[i]["ends"] - brother[i]["starts"])
        # 获取onnx的版本
        onnx_opset_version = model.opset_import[0].version
        if onnx_opset_version < 13:
            new_node = onnx.helper.make_node(
                "Split",
                name=f"{node.name}/split",
                inputs=[node.input[0]],
                outputs=[parent_node["node"].output[0] for parent_node in slice_nodes],
                axis=axis,
                split=splits,
            )
        else:
            split_param = onnx.helper.make_tensor(
                name=f"{node.name}/split_param",
                data_type=onnx.TensorProto.INT64,
                dims=[len(splits)],
                vals=splits,
            )
            model.graph.initializer.append(split_param)
            new_node = onnx.helper.make_node(
                "Split",
                name=f"{node.name}/split",
                inputs=[node.input[0], split_param.name],
                outputs=[parent_node["node"].output[0] for parent_node in slice_nodes],
                axis=axis,
            )
        new_nodes.append(new_node)
        processed.update(parent_node["node"].name for parent_node in slice_nodes)

    # 创建一个新的图
    new_graph = onnx.helper.make_graph(
        new_nodes,
        model.graph.name,
        model.graph.input,
        model.graph.output,
        model.graph.initializer,
    )

    # 替换原始的图
    model.graph.CopyFrom(new_graph)

    return model


def reshape_output(model: onnx.ModelProto) -> onnx.ModelProto:
    """修改onnx模型的输出尺寸，使得输出是4维的，在batch后填充1

    Args:
        model (onnx.ModelProto): 需要进行操作的onnx模型

    Returns:
        onnx.ModelProto: 修改后的onnx模型
    """
    model = onnx.load_model_from_string(model.SerializeToString())

    output_dims = infer_shapes(
        model, names=[output.name for output in model.graph.output]
    )

    output_node_map = {}
    for node in model.graph.node:
        for output in node.output:
            output_node_map[output] = node

    # 遍历模型的所有输出
    for output in model.graph.output:
        # 获取输出的维度
        dims = output_dims[output.name]
        # 如果维度不足4
        if len(dims) < 4:
            previous_node: onnx.NodeProto = output_node_map[output.name]
            previous_node_output_name = f"{previous_node.name}/output"
            previous_node.output.remove(output.name)
            previous_node.output.append(previous_node_output_name)

            new_shape = [-1] + [1] * (4 - len(dims)) + list(dims)[1:]
            reshape_param = onnx.helper.make_tensor(
                name=f"{output.name}_reshape_param",
                data_type=onnx.TensorProto.INT64,
                dims=[len(new_shape)],
                vals=new_shape,
            )
            # 创建一个新的Reshape操作
            reshape_node = onnx.helper.make_node(
                "Reshape",
                inputs=[previous_node_output_name, reshape_param.name],
                outputs=[output.name],
                name=f"{output.name}_reshape",
            )
            # 将新的Reshape操作和shape参数添加到图中
            model.graph.node.append(reshape_node)
            model.graph.initializer.append(reshape_param)
            for _ in range(len(dims)):
                output.type.tensor_type.shape.dim.pop()
            for dim in [dims[0], *new_shape[1:]]:
                output.type.tensor_type.shape.dim.append(
                    onnx.TensorShapeProto.Dimension(dim_value=dim)
                )

    return model


def rename(name):
    if name.startswith("/"):
        name = name[1:]
    name = name.replace("/", "_")
    name = name.replace(":", "_")
    return name


def simplify_name(model: onnx.ModelProto) -> onnx.ModelProto:
    """
    简化ONNX模型中的节点名称、节点输入输出以及initializers的名称。
    使用提供的rename函数来格式化名称。
    """
    model = onnx.load_model_from_string(model.SerializeToString())

    # 用来存储原始名称和更改后名称的映射
    name_map = {}

    for node in model.graph.node:
        node.name = rename(node.name)
        for i in range(len(node.input)):
            original_name = node.input[i]
            new_name = rename(original_name)
            node.input[i] = new_name
            name_map[original_name] = new_name
        for i in range(len(node.output)):
            original_name = node.output[i]
            new_name = rename(original_name)
            node.output[i] = new_name
            name_map[original_name] = new_name

    # 更新initializers的名称
    for initializer in model.graph.initializer:
        if initializer.name in name_map:
            initializer.name = name_map[initializer.name]

    return model


def add_reshape_after_matmul(
    model: onnx.ModelProto
) -> onnx.ModelProto:
    model = onnx.load_model_from_string(model.SerializeToString())
    matmul_dims = infer_shapes(model, ops=["MatMul"])
    for node in model.graph.node:
        if node.op_type == "MatMul":
            output_name = node.output[0]
            output_shape = matmul_dims[output_name]
            if len(output_shape) == 4:
                new_shape = [-1, *output_shape[1:]]
                temp_output_name = output_name + "_reshape"
                node.output[0] = temp_output_name
                reshape_param = onnx.helper.make_tensor(
                    name=f"{output_name}_reshape_param",
                    data_type=onnx.TensorProto.INT64,
                    dims=[len(new_shape)],
                    vals=new_shape,
                )
                reshape_node = onnx.helper.make_node(
                    "Reshape",
                    inputs=[temp_output_name, reshape_param.name],
                    outputs=[output_name],
                    name=temp_output_name,
                )
                model.graph.node.append(reshape_node)
                model.graph.initializer.append(reshape_param)
    return model


def process(args) -> None:
    onnx_model = onnx.load(args.onnx_file)
    for fn in args.modifiers:
        st.write(f"Applying {fn.__name__}")
        onnx_model: onnx.ModelProto = fn(onnx_model)
        if args.save_intermediate:
            st.download_button(
                f"Download result of {fn.__name__}",
                data=onnx_model.SerializeToString(),
                file_name=f"{args.onnx_file.name}_{fn.__name__}.onnx",
            )
    st.write("Checking model")
    onnx.checker.check_model(onnx_model)
    st.write("Finished")
    st.download_button(
        "Download result",
        data=onnx_model.SerializeToString(),
        file_name=f"{args.onnx_file.name}_result.onnx",
    )


def main():
    st.title("ONNX Modifier")
    onnx_file = st.file_uploader("Choose an ONNX file", type="onnx")
    if "modifiers" not in st.session_state:
        st.session_state.modifiers = []
    modifiers = [
        "replace_input_name",
        "replace_output_name",
        "simplify",
        "simplify_name",
        "modify_reshape",
        "replace_squeeze_and_unsqueeze",
        "resolve_reduce_mean_axis",
        "merge_slice",
        "reshape_output",
        "add_reshape_after_matmul",
    ]
    for modifier in [
        *modifiers,
        "All",
    ]:
        if st.button(f"Add {modifier}"):
            if modifier == "All":
                st.session_state.modifiers = [eval(modifier) for modifier in modifiers]
            else:
                st.session_state.modifiers.append(eval(modifier))
    st.write("Steps:", list(map(lambda x: x.__name__, st.session_state.modifiers)))
    if st.button("Clear modifiers"):
        st.session_state.modifiers = []
    save_intermediate = st.checkbox("Save intermediate results")
    if st.button("Start"):
        args = EasyDict(
            onnx_file=onnx_file,
            modifiers=st.session_state.modifiers,
            save_intermediate=save_intermediate,
        )
        process(args)


main()
