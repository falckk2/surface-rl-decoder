"""
This is an old version of the 2D Convolutional Model.
Its intended use is for loading the initial convolutional layers for transfer learning.

Implementation of an agent containing convolutional layers
followed by an LSTM to account for the time dependency
and linear layers to generate q values.
"""
import torch
import torch.nn as nn
import torch.nn.functional as F
from gtrxl_torch.gtrxl_torch import GTrXL
from surface_rl_decoder.syndrome_masks import plaquette_mask, vertex_mask


class SimpleConv2D(nn.Module):

    """
    Description:
        Third iteration of an agent. Consists of multiple 2D convolutional layers and an LSTM layer.
        Splits the input into x, z and both errors which it then proceeds to feed each part into a
        neural network before convolving them to a single feature map and adding them together,
        feeding them into the LSTM layer and finally into 2 linear layers
        from which only the final (with regards to time) output is used.
        For the instantiation it requires a dictionary containing the several
        parameters that the network will need to build itself.

    Parameters
    ==========
    config: dictionary containing configuration for the network. Expected keys:
        code_size: the code distance of the surface code
        num_actions_per_qubit: in most cases should be 3 but can be adjusted,
            it is the number of types of correction actions that can be applied on the qubit
        stack_depth: the length of the dimension with aspect to time
        input_channels: the size of the number of channels at the input.
            Should be 1 in most cases but can be adjusted if one wishes
        output_channels: the number of output channels from the first 2d convolution
        output_channels2: the number of output channels from the second 2d convolution
        output_channels3: the number of output channels from the third 2d convolution
        lstm_num_layers: the lstm network has a parameter which queries for
            how many lstm layers should be stacked on top of each other
        lstm_num_directions: can be 1 or 2, for uni- or bidirectional LSTM
        lstm_output_size: number of features in the hidden state of the LSTM;
            used synonymously as the size of the LSTM output vector
        neurons_lin_layer: number of neurons in the second-to-last linear layer

        kernel_size: the size of the convolution kernel
        padding_size: the amount of padding, should be a number
            such that the shortening in dimension length due to the kernel is negated
    """

    # pylint: disable=too-many-statements
    def __init__(self, config):
        super().__init__()
        print("Loading old 2D Conv agent")
        self.device = config.get("device")
        assert self.device is not None
        self.size = int(config.get("code_size"))
        # pylint: disable=not-callable
        self.plaquette_mask = torch.tensor(plaquette_mask, device=self.device)
        self.vertex_mask = torch.tensor(vertex_mask, device=self.device)

        self.nr_actions_per_qubit = int(config.get("num_actions_per_qubit"))
        self.stack_depth = int(config.get("stack_depth"))
        self.split_input_toggle = int(config.get("split_input_toggle", 0))

        self.input_channels = int(config.get("input_channels"))
        self.kernel_size = int(config.get("kernel_size"))
        self.output_channels = int(config.get("output_channels"))
        self.output_channels2 = int(config.get("output_channels2"))
        self.output_channels3 = int(config.get("output_channels3"))
        self.output_channels4 = int(config.get("output_channels4", 1))
        self.padding_size = int(config.get("padding_size"))
        self.use_lstm = int(config.get("use_lstm", 0))
        self.use_rnn = int(config.get("use_rnn", 0))
        self.use_transformer = int(config.get("use_gtrxl", 0))
        self.use_all_syndrome_layers = int(config.get("use_all_syndrome_layers", 0))

        if self.use_transformer:
            self.gtrxl_dim = int(config.get("gtrxl_dim"))
            self.gtrxl_heads = int(config.get("gtrxl_heads"))
            self.gtrxl_layers = int(config.get("gtrxl_layers"))
            self.gtrxl_hidden_dims = int(config.get("gtrxl_hidden_dims", 2048))
            self.gtrxl_rnn_layers = int(config.get("gtrxl_rnn_layers", 1))

        if self.use_lstm or self.use_rnn:
            self.lstm_num_layers = int(config.get("lstm_num_layers"))
            self.lstm_num_directions = int(config.get("lstm_num_directions"))
            assert self.lstm_num_directions in (1, 2)
            self.lstm_is_bidirectional = bool(self.lstm_num_directions - 1)
            self.lstm_output_size = int(config.get("lstm_output_size"))

        self.neurons_lin_layer1 = int(config.get("neurons_lin_layer1"))
        self.neurons_lin_layer2 = int(config.get("neurons_lin_layer2"))
        self.neurons_output = self.nr_actions_per_qubit * self.size * self.size + 1

        self.cnn_dimension = (self.size + 1) * (self.size + 1) * self.output_channels4

        self.input_conv_layer_both = nn.Conv2d(
            self.input_channels,
            self.output_channels,
            self.kernel_size,
            padding=self.padding_size,
        )

        self.nd_conv_layer_both = nn.Conv2d(
            self.output_channels,
            self.output_channels2,
            self.kernel_size,
            padding=self.padding_size,
        )

        self.rd_conv_layer_both = nn.Conv2d(
            self.output_channels2,
            self.output_channels3,
            self.kernel_size,
            padding=self.padding_size,
        )

        self.comp_conv_layer_both = nn.Conv2d(
            self.output_channels3,
            self.output_channels4,
            self.kernel_size,
            padding=self.padding_size,
        )
        if self.use_lstm:
            print("Using LSTM!")
            self.lstm_layer = nn.LSTM(
                (self.size + 1) * (self.size + 1) * self.output_channels4,
                self.lstm_output_size,
                num_layers=self.lstm_num_layers,
                bidirectional=self.lstm_is_bidirectional,
                batch_first=True,
            )

            lstm_total_output_size = self.lstm_output_size * self.lstm_num_directions
            if self.use_all_syndrome_layers:
                lstm_total_output_size *= self.stack_depth

            self.lin_layer1 = nn.Linear(lstm_total_output_size, self.neurons_lin_layer1)
        elif self.use_rnn:
            self.rnn_layer = nn.RNN(
                (self.size + 1) * (self.size + 1) * self.output_channels4,
                self.lstm_output_size,
                num_layers=self.lstm_num_layers,
                bidirectional=self.lstm_is_bidirectional,
                batch_first=True,
            )

            lstm_total_output_size = self.lstm_output_size * self.lstm_num_directions
            if self.use_all_syndrome_layers:
                lstm_total_output_size *= self.stack_depth

            self.lin_layer1 = nn.Linear(lstm_total_output_size, self.neurons_lin_layer1)
        elif self.use_transformer:
            self.grxl_dimension = self.cnn_dimension
            self.gated_transformer = GTrXL(
                d_model=self.grxl_dimension,
                nheads=self.gtrxl_heads,
                transformer_layers=self.gtrxl_layers,
                hidden_dims=self.gtrxl_hidden_dims,
                n_layers=self.gtrxl_rnn_layers,
            )

            gtrxl_total_output_size = self.grxl_dimension
            if self.use_all_syndrome_layers:
                gtrxl_total_output_size *= self.stack_depth
            self.lin_layer1 = nn.Linear(
                gtrxl_total_output_size, self.neurons_lin_layer1
            )

        else:
            self.lin_layer1 = nn.Linear(
                self.cnn_dimension * self.stack_depth, self.neurons_lin_layer1
            )

        self.lin_layer2 = nn.Linear(self.neurons_lin_layer1, self.neurons_lin_layer2)
        self.final_layer = nn.Linear(self.neurons_lin_layer2, self.neurons_output)

    def forward(self, state: torch.Tensor):
        """
        Perform a forward pass with the current batch of syndrome states.
        """
        # multiple input channels for different procedures,
        # they are then concatenated as the data is processed
        batch_size, timesteps, _, _ = state.size()
        print(f"{state.shape=}")
        state = state.view(
            -1, self.input_channels, (self.size + 1), (self.size + 1)
        )  # convolve both
        print(f"{state.shape=}")
        assert state.shape[0] == batch_size * timesteps, state.shape
        state = F.relu(self.input_conv_layer_both(state))
        state = F.relu(self.nd_conv_layer_both(state))
        state = F.relu(self.rd_conv_layer_both(state))
        state = F.relu(self.comp_conv_layer_both(state))

        complete = state.view(batch_size, self.stack_depth, self.cnn_dimension)

        assert complete.shape[0] == batch_size

        if self.use_lstm:
            output, (_, _) = self.lstm_layer(complete)

            assert (
                output.shape[2] == self.lstm_output_size * self.lstm_num_directions
            ), f"{output.shape=}, {self.lstm_output_size=}, {self.lstm_num_directions=}"
            assert len(output.shape) == 3

            if not self.use_all_syndrome_layers:
                output = output[:, -1, :]
        elif self.use_rnn:
            output, _ = self.rnn_layer(complete)
            assert (
                output.shape[2] == self.lstm_output_size * self.lstm_num_directions
            ), f"{output.shape=}, {self.lstm_output_size=}, {self.lstm_num_directions=}"
            assert len(output.shape) == 3

            if not self.use_all_syndrome_layers:
                output = output[:, -1, :]
        elif self.use_transformer:
            complete = complete.permute(1, 0, 2)
            output = self.gated_transformer(complete)
            if not self.use_all_syndrome_layers:
                output = output[-1, :, :]
            else:
                output = output.permute(1, 0, 2)

        else:
            output = complete.view(batch_size, -1)

        output = F.relu(
            self.lin_layer1(output)
        )  # take the last output feature vector from the lstm for each sample in the batch
        assert output.shape == (
            batch_size,
            self.neurons_lin_layer1,
        ), f"{output.shape=}, {(batch_size, self.neurons_lin_layer1)=}"

        output = F.relu(self.lin_layer2(output))
        final_output = self.final_layer(output)

        assert (
            final_output.shape[-1]
            == self.nr_actions_per_qubit * self.size * self.size + 1
        ), final_output.shape
        return final_output
