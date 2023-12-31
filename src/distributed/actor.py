"""
Define the actor process for exploration of the environment in
reinforcement learning.
"""
import json
import os
from copy import deepcopy
from time import time
from collections import namedtuple
import logging
import numpy as np

# pylint: disable=not-callable
import torch
from torch.utils.tensorboard import SummaryWriter
from torch.nn.utils import vector_to_parameters
from distributed.environment_set import EnvironmentSet
from distributed.model_util import choose_model, extend_model_config, load_model
from distributed.util import (
    COORDINATE_SHIFTS,
    LOCAL_DELTAS,
    anneal_factor,
    compute_priorities,
    format_torch,
    independent_step,
    multiple_independent_steps,
    select_actions,
    select_actions_value_network,
    time_tb,
)
from surface_rl_decoder.surface_code_util import SOLVED_EPISODE_REWARD
from surface_rl_decoder.surface_code import SurfaceCode
from surface_rl_decoder.syndrome_masks import get_plaquette_mask, get_vertex_mask

# pylint: disable=too-many-statements,too-many-locals,too-many-branches

Transition = namedtuple(
    "Transition", ["state", "action", "reward", "next_state", "terminal"]
)
VTransition = namedtuple(
    "VTransition",
    [
        "state",
        "action",
        "reward",
        "next_state",
        "terminal",
        "optimal_action",
        "optimal_reward",
        "optimal_next_state",
        "optimal_terminal",
    ],
)


def actor(args):
    """
    Define the actor function to be run by a mp process.
    The actor defines multiple environments which are in differing states
    and can perform steps independent of each other.

    After a certain number of steps, the used policy network is updated
    with new parameters from the learner process.

    Parameters
    ==========
    args: dictionary containing actor configuration
        "actor_io_queue": mp.Queue object for communication between actor
            and replay memory
        "learner_actor_queue": mp.Queue object for communication between actor
            and learner process
        "num_environments": (int) number of independent environments to perform steps in
        "size_action_history": (int) maximum size of the action history of the environment,
            trying to execute more actions than this in one environment causes the environment
            to terminate and start again with a new syndrome.
        "size_local_memory_buffer":  (int) maximum number of objects in the local
            memory store for transitions, actions, q values, rewards
        "num_actions_per_qubit": (int) number of possible operators on a qubit,
            default should be 3, for Pauli-X, -Y, -Z
        "verbosity": verbosity level
        "epsilon": (float) probability to choose a random action
        "model_name": (str) specifier for the model
        "model_config": (dict) configuration for network architecture.
            May change with different architectures
        "benchmarking": whether certain performance time measurements should be performed
        "summary_path": (str), base path for tensorboard
        "summary_date": (str), target path for tensorboard for current run
        "load_model": toggle whether to load a pretrained model
        "old_model_path" if 'load_model' is activated, this is the location from which
            the old model is loaded
        "discount_factor": gamma factor in reinforcement learning
        "discount_intermediate_reward": the discount factor dictating how strongly
            lower layers should be discounted when calculating the reward for
            creating/destroying syndromes
        "min_value_factor_intermediate_reward": minimum value that the effect
            of the intermediate reward should be annealed to
        "decay_factor_intermediate_reward": how strongly the intermediate reward should
            decay over time during a training run
        "decay_factor_epsilon": how strongly the exploration factor ε should decay
            over time during a training run
        "min_value_factor_epsilon": minimum value that the exploration factor ε
            should be annealed to
    """
    num_environments = args["num_environments"]
    actor_id = args["id"]
    size_action_history = args["size_action_history"]
    device = args["device"]
    verbosity = args["verbosity"]
    benchmarking = args["benchmarking"]
    num_actions_per_qubit = args["num_actions_per_qubit"]
    epsilon = args["epsilon"]
    load_model_flag = args["load_model"]
    old_model_path = args["old_model_path"]
    discount_factor = args["discount_factor"]
    discount_factor_anneal = args["discount_factor_anneal"]
    discount_factor_start = args["discount_factor_start"]
    rl_type = args["rl_type"]
    p_error = args["p_error"]
    p_msmt = args["p_msmt"]
    p_error_start = args["p_error_start"]
    p_msmt_start = args["p_msmt_start"]
    p_error_anneal = args["p_error_anneal"]
    p_msmt_anneal = args["p_msmt_anneal"]
    discount_intermediate_reward = float(args.get("discount_intermediate_reward", 0.75))
    min_value_factor_intermediate_reward = float(
        args.get("min_value_intermediate_reward", 0.0)
    )
    decay_factor_intermediate_reward = float(
        args.get("decay_factor_intermediate_reward", 1.0)
    )
    decay_factor_epsilon = float(args.get("decay_factor_epsilon", 1.0))
    min_value_factor_epsilon = float(args.get("min_value_factor_epsilon", 0.0))
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(f"actor_{actor_id}")
    if verbosity >= 4:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)
    logger.info("Fire up all the environments!")

    seed = int(args.get("seed", 0))

    if seed != 0:
        np.random.seed(seed + actor_id)
        torch.manual_seed(seed + actor_id)
        torch.cuda.manual_seed(seed + actor_id)
        torch.cuda.manual_seed_all(seed + actor_id)

    env = SurfaceCode()
    state_size = env.syndrome_size
    code_size = state_size - 1
    stack_depth = env.stack_depth

    # create a collection of independent environments
    environments = EnvironmentSet(env, num_environments)
    if rl_type == "v":
        transition_type = np.dtype(
            [
                ("state", (np.uint8, (stack_depth, state_size, state_size))),
                ("action", (np.uint8, 3)),
                ("reward", float),
                ("next_state", (np.uint8, (stack_depth, state_size, state_size))),
                ("terminal", bool),
                ("optimal_action", (np.uint8, 3)),
                ("optimal_reward", float),
                (
                    "optimal_next_state",
                    (np.uint8, (stack_depth, state_size, state_size)),
                ),
                ("optimal_terminal", bool),
            ]
        )
    else:
        transition_type = np.dtype(
            [
                ("state", (np.uint8, (stack_depth, state_size, state_size))),
                ("action", (np.uint8, 3)),
                ("reward", float),
                ("next_state", (np.uint8, (stack_depth, state_size, state_size))),
                ("terminal", bool),
            ]
        )

    # initialize all states
    p_error_start_list = np.repeat(p_error_start, num_environments)
    p_msmt_start_list = np.repeat(p_msmt_start, num_environments)
    states = environments.reset_all(
        p_error=p_error_start_list, p_msmt=p_msmt_start_list
    )
    steps_per_episode = np.zeros(num_environments)

    # initialize local memory buffers
    size_local_memory_buffer = args["size_local_memory_buffer"] + 1
    local_buffer_transitions = np.empty(
        (num_environments, size_local_memory_buffer), dtype=transition_type
    )
    local_buffer_actions = np.empty(
        (num_environments, size_local_memory_buffer, 3), dtype=np.uint8
    )
    if rl_type == "q":
        local_buffer_qvalues = np.empty(
            (num_environments, size_local_memory_buffer),
            dtype=(float, num_actions_per_qubit * code_size * code_size + 1),
        )
    elif rl_type == "v":
        local_buffer_qvalues = np.empty(
            (num_environments, size_local_memory_buffer),
            dtype=float,
        )
    local_buffer_rewards = np.empty(
        (num_environments, size_local_memory_buffer), dtype=float
    )
    buffer_idx = 0

    # load communication queues
    actor_io_queue = args["actor_io_queue"]
    learner_actor_queue = args["learner_actor_queue"]

    # initialize the policy agent
    model_name = args["model_name"]
    model_config = args["model_config"]
    model_config = extend_model_config(
        model_config, state_size, stack_depth, device=device
    )
    base_model_config_path = args["base_model_config_path"]
    base_model_path = args["base_model_path"]
    use_transfer_learning = args["use_transfer_learning"]

    if rl_type == "v":
        vertex_mask = get_vertex_mask(code_size)
        plaquette_mask = get_plaquette_mask(code_size)
        combined_mask = np.logical_or(vertex_mask, plaquette_mask, dtype=np.int8)
        combined_mask = format_torch(combined_mask, device=device, dtype=torch.int8)

    # prepare Transfer learning, if enabled
    if use_transfer_learning:
        logger.info(f"Prepare transfer learning for d={code_size}.")
        with open(base_model_config_path, "r") as json_file:
            base_model_config = json.load(json_file)["simple_conv"]

        base_model_config = extend_model_config(
            base_model_config, state_size, stack_depth, device=device
        )
    else:
        base_model_config = None

    model = choose_model(
        model_name,
        model_config,
        model_path_base=base_model_path,
        model_config_base=base_model_config,
        transfer_learning=use_transfer_learning,
        rl_type=rl_type,
    )

    if load_model_flag:
        model, _, _ = load_model(model, old_model_path)
        logger.info(f"Loaded actor model from {old_model_path}")

    model.to(device)

    performance_start = time()
    heart = time()
    heartbeat_interval = 60  # seconds

    logger.info(f"Actor {actor_id} starting loop on device {device}")
    sent_data_chunks = 0

    # initialize tensorboard for monitoring/logging
    summary_path = args["summary_path"]
    summary_date = args["summary_date"]
    tensorboard = SummaryWriter(
        os.path.join(summary_path, str(code_size), summary_date, "actor")
    )
    tensorboard_step = 0
    steps_to_benchmark = 0
    benchmark_frequency = 1000

    # pylint: disable=too-many-nested-blocks

    # start the main exploration loop
    while True:
        steps_per_episode += 1
        steps_to_benchmark += 1

        # select actions based on the chosen model and latest states
        _states = torch.tensor(states, dtype=torch.float32, device=device)
        select_action_start = time()
        current_time_tb = time_tb()
        delta_t = select_action_start - performance_start

        annealed_epsilon = anneal_factor(
            delta_t,
            decay_factor=decay_factor_epsilon,
            min_value=min_value_factor_epsilon,
            base_factor=epsilon,
        )

        current_p_error = anneal_factor(
            delta_t,
            decay_factor=p_error_anneal,
            min_value=p_error_start,
            max_value=p_error,
            base_factor=p_error_start,
        )

        current_p_msmt = anneal_factor(
            delta_t,
            decay_factor=p_msmt_anneal,
            min_value=p_msmt_start,
            max_value=p_msmt,
            base_factor=p_msmt_start,
        )

        current_discount_factor = anneal_factor(
            delta_t,
            decay_factor=discount_factor_anneal,
            min_value=discount_factor_start,
            max_value=discount_factor,
            base_factor=discount_factor_start,
        )

        if rl_type == "q":
            actions, q_values = select_actions(
                _states, model, state_size - 1, epsilon=annealed_epsilon
            )
        elif rl_type == "v":
            # call values here the same as q values, although they are actually
            # plain values
            (
                actions,
                q_values,
                optimal_actions,
                optimal_q_values,
            ) = select_actions_value_network(
                _states,
                model,
                code_size,
                stack_depth,
                combined_mask,
                COORDINATE_SHIFTS,
                LOCAL_DELTAS,
                device=device,
                epsilon=epsilon,
            )

            q_values = np.squeeze(q_values)
            optimal_q_values = np.squeeze(optimal_q_values)

        if benchmarking and steps_to_benchmark % benchmark_frequency == 0:
            select_action_stop = time()
            logger.info(
                f"time for select action: {select_action_stop - select_action_start}"
            )

        if verbosity >= 2:
            tensorboard.add_scalars(
                "actor/parameters",
                {
                    "annealed_epsilon": annealed_epsilon,
                    "annealed_p_error": current_p_error,
                    "annealed_p_msmt": current_p_msmt,
                    "annealed_discount_factor": current_discount_factor,
                },
                delta_t,
                walltime=current_time_tb,
            )

        # perform the chosen actions
        steps_start = time()

        annealing_intermediate_reward = anneal_factor(
            delta_t,
            decay_factor=decay_factor_intermediate_reward,
            min_value=min_value_factor_intermediate_reward,
        )

        if rl_type == "v":
            qubits = [
                deepcopy(environments.environments[i].qubits)
                for i in range(num_environments)
            ]
            syndrome_errors = [
                deepcopy(environments.environments[i].syndrome_errors)
                for i in range(num_environments)
            ]
            actual_errors = [
                deepcopy(environments.environments[i].actual_errors)
                for i in range(num_environments)
            ]
            action_histories = [
                deepcopy(environments.environments[i].actions)
                for i in range(num_environments)
            ]

            (
                optimal_next_states,
                optimal_rewards,
                optimal_terminals,
                _,
            ) = multiple_independent_steps(
                states,
                qubits,
                optimal_actions,
                vertex_mask,
                plaquette_mask,
                syndrome_errors,
                actual_errors,
                action_histories,
                discount_intermediate_reward=discount_intermediate_reward,
                annealing_intermediate_reward=annealing_intermediate_reward,
                punish_repeating_actions=0,
            )

        next_states, rewards, terminals, _ = environments.step(
            actions,
            discount_intermediate_reward=discount_intermediate_reward,
            annealing_intermediate_reward=annealing_intermediate_reward,
            punish_repeating_actions=0,
        )

        if benchmarking and steps_to_benchmark % benchmark_frequency == 0:
            steps_stop = time()
            logger.info(
                f"time to step through environments: {steps_stop - steps_start}"
            )

        if verbosity >= 2:
            current_time_tb = time_tb()
            tensorboard.add_scalars(
                "actor/effect_intermediate_reward",
                {"anneal_factor": annealing_intermediate_reward},
                delta_t,
                walltime=current_time_tb,
            )

        # save transitions to local buffer
        if rl_type == "v":
            transitions = np.asarray(
                [
                    VTransition(
                        states[i],
                        actions[i],
                        rewards[i],
                        next_states[i],
                        terminals[i],
                        optimal_actions[i],
                        optimal_rewards[i],
                        optimal_next_states[i],
                        optimal_terminals[i],
                    )
                    for i in range(num_environments)
                ],
                dtype=transition_type,
            )
        else:
            transitions = np.asarray(
                [
                    Transition(
                        states[i], actions[i], rewards[i], next_states[i], terminals[i]
                    )
                    for i in range(num_environments)
                ],
                dtype=transition_type,
            )

        local_buffer_transitions[:, buffer_idx] = transitions
        local_buffer_actions[:, buffer_idx] = actions
        local_buffer_qvalues[:, buffer_idx] = q_values
        local_buffer_rewards[:, buffer_idx] = rewards
        buffer_idx += 1

        # prepare to send local transitions to replay memory
        if buffer_idx >= size_local_memory_buffer:
            # get new weights for the policy model here
            if (learner_qsize := learner_actor_queue.qsize()) > 0:
                # consume all the deprecated updates without effect
                for _ in range(learner_qsize - 1):
                    learner_actor_queue.get()
                msg, network_params = learner_actor_queue.get()
                assert msg is not None
                assert network_params is not None
                if msg == "network_update":
                    logger.info(
                        f"Actor {actor_id} received new network weights. "
                        f"Taken the latest of {learner_qsize} updates."
                    )
                    vector_to_parameters(network_params, model.parameters())
                    model.to(device)
            if rl_type == "v":
                local_buffer_qvalues = local_buffer_qvalues.reshape(
                    (num_environments, -1)
                )

            new_local_qvalues = np.roll(local_buffer_qvalues, -1, axis=1)

            if rl_type == "v":
                original_shape = local_buffer_qvalues.shape
                local_buffer_qvalues = local_buffer_qvalues.reshape(
                    (num_environments, -1, 1)
                )
                new_local_qvalues = new_local_qvalues.reshape((num_environments, -1, 1))

            priorities = compute_priorities(
                local_buffer_actions[:, :-1],
                local_buffer_rewards[:, :-1],
                local_buffer_qvalues[:, :-1],
                new_local_qvalues[:, :-1],
                current_discount_factor,
                code_size,
                rl_type=rl_type,
            )

            if rl_type == "v":
                local_buffer_qvalues = local_buffer_qvalues.reshape(original_shape)
                new_local_qvalues = new_local_qvalues.reshape(original_shape)

            # this approach counts through all environments and local memory buffer continuously
            # with no differentiation between those two channels
            to_send = [
                *zip(local_buffer_transitions[:, :-1].flatten(), priorities.flatten())
            ]

            # TODO: this was purely for debugging. Can remove
            for elements in to_send:
                for anything in elements:
                    # pylint: disable=bare-except
                    try:
                        for something in anything:
                            assert (
                                something is not None
                            ), f"{elements=}, {anything=}, {something=}"
                    except:
                        assert anything is not None, f"{elements=}, {anything=}"

            logger.debug("Put data in actor_io_queue")
            actor_io_queue.put(to_send)
            if verbosity >= 4:
                sent_data_chunks += buffer_idx
                current_time_tb = time_tb()
                tensorboard.add_scalar(
                    "actor/sent_data_chunks",
                    sent_data_chunks,
                    delta_t,
                    walltime=current_time_tb,
                )
                tensorboard_step += 1

            buffer_idx = 0

        # determine episodes which are to be deemed terminal
        too_many_steps = steps_per_episode > size_action_history
        if np.any(terminals) or np.any(too_many_steps):
            # find terminal envs
            indices = np.argwhere(np.logical_or(terminals, too_many_steps)).flatten()
            p_error_list = np.repeat(current_p_error, num_environments)
            p_msmt_list = np.repeat(current_p_msmt, num_environments)

            reset_states = environments.reset_terminal_environments(
                indices=indices, p_error=p_error_list, p_msmt=p_msmt_list
            )
            next_states[indices] = reset_states[indices]
            steps_per_episode[indices] = 0

        # update states for next iteration
        states = next_states
        environments.states = deepcopy(states)

        if time() - heart > heartbeat_interval:
            heart = time()
            logger.debug("It's alive, can you feel it?")
