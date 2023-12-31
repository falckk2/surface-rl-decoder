"""
Simple script to perform analysis on already-trained models.

This script is divided into different parts which can be triggered by
passing the correct flag upon runtime [--run_evaluation, --produce_plots, etc.]

Run 'python analyze_threshold.py --help' for more information about available flags and parameters.
"""
from glob import glob
from typing import Dict, List
from analysis.analysis_util import analyze_succesful_episodes
import sys
import os
import subprocess
import traceback
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import torch
import yaml
import json
from analysis.training_run_class import TrainingRun
from argparse import ArgumentParser
import logging

from distributed.model_util import choose_model, choose_old_model, load_model

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("post-run-eval")

CLUSTER_BASE_PATH = "/cephyr/NOBACKUP/groups/snic2021-23-319/"
CLUSTER_NETWORK_PATH = "networks/"
CLUSTER_RESULT_PATH = "analysis/"
LOCAL_NETWORK_PATH = "threshold_networks"
plt.rcParams.update({"font.size": 18})

RUN_LOCAL = False
parser = ArgumentParser()
parser.add_argument("--base_path", default=CLUSTER_BASE_PATH, nargs="?")
parser.add_argument(
    "--network_path", default=CLUSTER_BASE_PATH + CLUSTER_NETWORK_PATH, nargs="?"
)
parser.add_argument(
    "--result_path", default=CLUSTER_BASE_PATH + CLUSTER_RESULT_PATH, nargs="?"
)
parser.add_argument("--do_copy", action="store_true")
parser.add_argument("--run_evaluation", action="store_true")
parser.add_argument("--load_eval_results", action="store_true")
parser.add_argument("--produce_plots", action="store_true")
parser.add_argument("--n_episodes", default=256, nargs="?")
parser.add_argument("--max_recursion", default=20, nargs="?")
parser.add_argument("--max_steps", default=40, nargs="?")
parser.add_argument("--runs_config", default="", nargs="?")
parser.add_argument("--eval_job_id", default=None, nargs="?")
parser.add_argument("--merge_dfs", action="store_true")
parser.add_argument("--p_start", default=0.0001, nargs="?")
parser.add_argument("--p_stop", default=0.016, nargs="?")
parser.add_argument("--p_step", default=0.0005, nargs="?")

args = parser.parse_args()

base_path = args.base_path
network_path = args.network_path
result_path = args.result_path
run_evaluation = args.run_evaluation
load_eval_results = args.load_eval_results
produce_plots = args.produce_plots
do_copy = args.do_copy
n_episodes = int(args.n_episodes)
max_num_of_steps = int(args.max_steps)
runs_config = args.runs_config
evaluation_job_id = args.eval_job_id
merge_dfs = args.merge_dfs
max_recursion = int(args.max_recursion)
p_start = float(args.p_start)
p_stop = float(args.p_stop)
p_step = float(args.p_step)

# define list of runs to analyze
if runs_config == "":
# define default case
    training_runs = [
        TrainingRun(
            69366,
            3,
            3,
            0.05,
            0.05,
            "q",
            "3D Conv",
            model_name="conv3d",
            model_location=network_path,
        ),
        TrainingRun(
            69312,
            5,
            5,
            0.01,
            0.01,
            "q",
            "3D Conv",
            model_name="conv3d",
            model_location=network_path,
        ),
        TrainingRun(
            69545,
            7,
            7,
            0.005,
            0.005,
            "q",
            "3D Conv",
            model_name="conv3d",
            model_location=network_path,
        ),
        TrainingRun(
            71571,
            9,
            9,
            0.005,
            0.005,
            "q",
            "3D Conv",
            model_name="conv3d",
            model_location=network_path,
        ),
    ]
else:
    # read configuration file to define trained models to be loaded
    with open(runs_config, "r") as jsonfile:
        analysis_runs = json.load(jsonfile)
        training_runs = []
        for run in analysis_runs:
            training_runs.append(
                TrainingRun(
                    run.get("job_id"),
                    run.get("code_size"),
                    run.get("stack_depth"),
                    run.get("p_err"),
                    run.get("p_msmt"),
                    run.get("rl_type"),
                    run.get("model_type"),
                    model_name=run.get("model_name"),
                    model_location=run.get("model_localtion", network_path),
                )
            )

if do_copy:
    # If models aren't available locally, copy them from the cluster.
    # Careful: hard-coded
    logger.info("Copy Data from Cluster")

    for run in training_runs:
        logger.info(f"\tCopying {run.job_id}...")
        try:
            target_path = f"{LOCAL_NETWORK_PATH}/{run.code_size}"
            os.makedirs(target_path, exist_ok=True)
            command = f"scp -r alvis://cephyr/users/gunter/Alvis/surface-rl-decoder/{CLUSTER_NETWORK_PATH}/{run.code_size}/{run.job_id} {target_path}"
            process = subprocess.run(command.split(), stdout=subprocess.PIPE)
            logger.debug(f"{target_path}")
        except Exception as err:
            logger.error(err)
            continue

df_all_stats = pd.DataFrame(
    columns=["jobid", "code_size", "stack_depth", "p_err_train", "p_err"]
)

all_results_counter = 0
eval_device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
if torch.cuda.is_available():
    LOCAL_NETWORK_PATH = "/surface-rl-decoder/networks"

if evaluation_job_id is not None:
    csv_file_path = os.path.join(
        result_path, f"threshold_analysis_results_{evaluation_job_id}.csv"
    )
else:
    csv_file_path = os.path.join(result_path, "threshold_analysis_results.csv")

if run_evaluation:
    # Run the actual evaluation.
    # generate multiple random episodes at increasing error rates
    # for the agent to solve
    logger.info("Proceed to Evaluation")
    p_error_list = np.arange(start=p_start, stop=p_stop, step=p_step)

    for run in training_runs:
        os.environ["CONFIG_ENV_SIZE"] = str(run.code_size)
        os.environ["CONFIG_ENV_STACK_DEPTH"] = str(run.stack_depth)

        model_config_path = os.path.join(
            run.model_location,
            str(run.code_size),
            str(run.job_id),
            f"{run.model_name}_{run.code_size}_meta.yaml",
        )

        old_model_path = os.path.join(
            run.model_location,
            str(run.code_size),
            str(run.job_id),
            f"{run.model_name}_{run.code_size}_{run.job_id}.pt",
        )

        with open(model_config_path, "r") as yaml_file:
            general_config = yaml.load(yaml_file)
            model_config = general_config["network"]
            model_config["device"] = "cuda" if torch.cuda.is_available() else "cpu"
            p_err_train = general_config["global"]["env"]["p_error"]
        # load model
        logger.info("Load Model")
        try:
            if int(run.job_id) < 70000:
                model = choose_old_model(run.model_name, model_config)
            else:
                model = choose_model(run.model_name, model_config, rl_type=run.rl_type)

            model, _, _ = load_model(model, old_model_path, model_device=eval_device)

        except Exception as err:
            error_traceback = traceback.format_exc()
            logger.error("An error occurred!")
            logger.error(error_traceback)

            continue
        
        logger.info(
            f"Code size = {run.code_size}, Job ID: {run.job_id}, Iterate over p_err..."
        )
        for p_idx, p_err in enumerate(p_error_list):
            sys.stdout.write(f"\r{p_idx + 1:02d} / {len(p_error_list):02d}")
            p_msmt = p_err

            result_dict: Dict = analyze_succesful_episodes(
                model,
                "",
                device=eval_device,
                total_n_episodes=n_episodes,
                max_num_of_steps=max_num_of_steps,
                discount_intermediate_reward=0.3,
                verbosity=2,
                p_msmt=p_msmt,
                p_err=p_err,
                code_size=run.code_size,
                stack_depth=run.stack_depth,
                max_recursion=max_recursion
            )

            result_dict["n_ground_states"] = result_dict["n_valid_ground_states"]
            assert result_dict["n_ep_w_loops"] == result_dict["n_valid_non_trivial_loops"], f'{result_dict["n_ep_w_loops"]=}, {result_dict["n_valid_non_trivial_loops"]=}, {result_dict=}'
            result_dict["n_valid_non_trivial_loops"] = result_dict["n_ep_w_loops"]

            if result_dict['n_too_long'] > 0:
                logger.info(f"{result_dict['n_too_long']=}")
            assert (
                result_dict["n_valid_episodes"] == n_episodes
            ), f"{result_dict['n_valid_episodes']}, {result_dict}"


            assert (
                result_dict["total_n_episodes"] == n_episodes
            ), f"{result_dict['total_n_episodes']=}"
            assert (
                result_dict["n_valid_ground_states"] + result_dict["n_valid_non_trivial_loops"]
                == n_episodes
            ), f"{result_dict['n_valid_ground_states']=}, {result_dict['n_valid_non_trivial_loops']=}, {result_dict['n_too_long']=}, {result_dict['n_ep_w_loops']=}\n{result_dict}"

            result_dict["jobid"] = run.job_id
            result_dict["code_size"] = run.code_size
            result_dict["stack_depth"] = run.stack_depth
            result_dict["p_err_train"] = p_err_train
            result_dict["p_err"] = p_err
            result_dict["avg_steps"] = result_dict["n_steps_arr"].mean()
            result_dict.pop("n_steps_arr")

            # save relevant eval stats to dataframe
            df_all_stats = df_all_stats.append(result_dict, ignore_index=True)

    logger.info("Saving dataframe...")
    if os.path.exists(csv_file_path):
        df_all_stats.to_csv(csv_file_path, mode="a", header=False)
    else:
        df_all_stats.to_csv(csv_file_path)

if load_eval_results:
# load pre-existing evaluation results
    if merge_dfs:
        logger.info("Load multiple data files and merge them")
        df_path_list = glob(result_path + "threshold_analysis_results_*")
        print(f"{result_path=}, {df_path_list=}")
        df_list = [pd.read_csv(df_path, index_col=0) for df_path in df_path_list]
        df_all_stats = pd.concat(df_list, ignore_index=True)
    else:
        logger.info("Load data file")
        df_all_stats = pd.read_csv(csv_file_path, index_col=0)

    logger.debug(f"{df_all_stats=}")

if not produce_plots:
    logger.info("Not producing result plot. Exiting...")
    sys.exit()
logger.debug(f"{df_all_stats=}")
# split df into sensible groups

dfs: List[pd.DataFrame] = [
    df_all_stats.loc[
        (df_all_stats["jobid"] == run.job_id)
        | (df_all_stats["jobid"] == str(run.job_id))
    ].copy(deep=True)
    for run in training_runs
]

new_dfs = []

eval_key_list = [
    "total_n_episodes",
    "n_ground_states",
    "n_valid_episodes",
    "n_valid_ground_states",
    "n_valid_non_trivial_loops",
    "n_ep_w_syndromes",
    "n_ep_w_loops",
    "n_too_long",
    "n_too_long_w_loops",
    "n_too_long_w_syndromes",
    "avg_steps",
]

agg_key_list = [key for key in eval_key_list]

for df in dfs:
    print(df)
    if df.shape[0] == 0 or df.shape[1] == 0:
        continue
    logger.debug(f"{df['jobid'].iloc[0]}, {df=}")

    df = df.sort_values(by="n_ground_states", ascending=True)

    df["expected_n_err"] = (
        df["p_err"] * df["code_size"] * df["code_size"] * df["stack_depth"]
    )
    df["p_err_one_layer"] = df["p_err"] * df["stack_depth"]
    df["avg_steps"] = df["avg_steps"] * df["total_n_episodes"]

    aggregation_dict = {agg_key: ["sum"] for agg_key in agg_key_list}
    aggregation_dict["code_size"] = ["last"]
    aggregation_dict["stack_depth"] = ["last"]
    aggregation_dict["p_err"] = ["last"]
    aggregation_dict["expected_n_err"] = ["last"]
    aggregation_dict["p_err_one_layer"] = ["last"]

    groups = df.groupby(by="p_err")
    agg_groups = groups.agg(aggregation_dict)

    new_df = pd.DataFrame()

    agg_groups["weighted_avg_steps"] = (
        agg_groups["avg_steps"] / agg_groups["total_n_episodes"]
    )

    agg_groups.columns = agg_groups.columns.droplevel(1)

    logger.debug(f"{agg_groups=}")

    agg_groups["logical_err_rate"] = (
        agg_groups["n_ep_w_loops"] / agg_groups["total_n_episodes"]
    )


    agg_groups["valid_success_rate"] = (
        agg_groups["n_valid_ground_states"] / agg_groups["n_valid_episodes"]
    )
    agg_groups["overall_success_rate"] = (
        agg_groups["n_ground_states"] + agg_groups["n_ep_w_syndromes"]
    ) / agg_groups["total_n_episodes"]

    agg_groups["valid_fail_rate"] = 1.0 - agg_groups["valid_success_rate"]
    agg_groups["overall_fail_rate"] = 1.0 - agg_groups["overall_success_rate"]

    agg_groups["valid_fail_rate_per_cycle"] = (
        agg_groups["valid_fail_rate"] / agg_groups["stack_depth"]
    )
    agg_groups["overall_fail_rate_per_cycle"] = (
        agg_groups["overall_fail_rate"] / agg_groups["stack_depth"]
    )
    agg_groups["logical_err_rate_per_cycle"] = (
        agg_groups["logical_err_rate"] / agg_groups["stack_depth"]
    )

    agg_groups["validity_rate"] = (
        agg_groups["n_valid_episodes"] / agg_groups["total_n_episodes"]
    )

    agg_groups["valid_avg_lifetime"] = 1.0 / agg_groups["valid_fail_rate_per_cycle"]
    agg_groups["overall_avg_lifetime"] = 1.0 / agg_groups["overall_fail_rate_per_cycle"]
    agg_groups["logical_avg_lifetime"] = 1.0 / agg_groups["logical_err_rate_per_cycle"]

    agg_groups["total_nominal_episodes"] = agg_groups["total_n_episodes"] - agg_groups["n_ep_w_syndromes"]
    assert np.all(agg_groups["total_nominal_episodes"] >= 0), f'{agg_groups["total_nominal_episodes"]=}, {agg_groups["total_n_episodes"]=}, {agg_groups["n_too_long_w_syndromes"]=}, {agg_groups["n_ep_w_syndromes"]}'
    agg_groups["successful_nominal_episodes"] = agg_groups["n_valid_ground_states"] / agg_groups["total_nominal_episodes"]
    agg_groups["nominal_fail_rate_per_cycle"] = (1.0 - agg_groups["successful_nominal_episodes"]) / agg_groups["stack_depth"]

    new_dfs.append(agg_groups)

df_all = pd.concat(new_dfs)

df_all.to_csv("test_results_combined.csv")

# load data from previous paper for comparison
sweke_data = pd.read_csv(
    "plots/sweke_lifetime_datapoints.csv", index_col=None, header=0
)

max_x = max(new_dfs[0]["p_err"].max(), sweke_data["p_err"].max())

error_rates = df_all["p_err"]
key_success_rate = "weighted_success_rate"
title_succes_rate = "Success Rate"
key_scaled_fail_rate = "overall_fail_rate_per_cycle"
title_scaled_fail_rate = "Overall Fail Rate Per Cycle"

key_valid_fail_rate = "valid_fail_rate_per_cycle"
key_valid_fail_rate = "nominal_fail_rate_per_cycle"
title_valid_fail_rate = "Fail Rate Per Cycle"

key_valid_avg_life = "valid_avg_lifetime"
title_valid_avg_life = "Average Lifetime"

key_overall_avg_life = "overall_avg_lifetime"
title_overall_avg_life = "Overall Average Lifetime"

key_logical_err_rate = "logical_err_rate_per_cycle"
title_logical_err_rate = "Logical Error Rate"

key_logical_lifetime = "logical_avg_lifetime"
title_logical_lifetime = "Average Lifetime"


plot_colors = ["#404E5C", "#F76C5E", "#E9B44C", "#7F95D1", "#CF1259"]
markers = ["o", "v", "^", "X", "d"]
ylim_lin_plot = (-1e-4, 0.008)
ylim_log_plot = (50, 1e5)


def set_text_lin(axis):
    axis.text(0.0055, 0.0052, "Single Qubit", rotation=49)


def set_text_lin_split(axis):
    axis.text(0.0053, 0.0044, "Single Qubit", rotation=42)


def set_text_log(axis):
    axis.text(0.0015, 125, "Single Qubit", rotation=-15)


def set_text_log_split(axis):
    axis.text(0.0015, 100, "Single Qubit", rotation=-15)

# do the actual plotting
# further below, saving can be enabled and the path can be defined; also hard-coded
if True:
    ################## Plot Valid Fail Rate per Cycle Log Scale ##################
    fig, axes = plt.subplots(
        2,
        1,
        sharex=True,
        gridspec_kw={"height_ratios": [4, 1], "wspace": 0, "hspace": 0.05},
    )
    # ax = axes
    ax = axes[0]
    ax1 = axes[1]

    for i, run in enumerate(training_runs):
        # if run.code_size == 13: continue
        code_size = new_dfs[i]["code_size"].iloc[0]
        stack_depth = new_dfs[i]["stack_depth"].iloc[0]
        # print(new_dfs[i])
        y_error = np.sqrt(
            new_dfs[i][key_valid_fail_rate]
            * (1.0 - new_dfs[i][key_valid_fail_rate])
            / new_dfs[i]["n_valid_episodes"]
        )
        log_y_error = 0.434 * y_error
        ax.errorbar(
            x=new_dfs[i]["p_err"]
            + np.random.normal(loc=0, scale=1.5e-5, size=len(new_dfs[i]["p_err"])),
            y=new_dfs[i][key_valid_fail_rate],
            yerr=log_y_error,
            fmt=".",
            linewidth=1.5,
            markersize=0,
            # c=plot_colors[i],
            # marker=markers[i],
        )

        ax.scatter(
            x=new_dfs[i]["p_err"],
            y=new_dfs[i][key_valid_fail_rate],
            label=r"$d=h=$" + f"{code_size}",
            # s=100
            # * (new_dfs[i]["n_valid_episodes"] / new_dfs[i]["total_n_episodes"]) ** 1.2,
            # c=plot_colors[i],
            # marker=markers[i],
        )

        # plot disregard-fraction
        ax1.scatter(
            x=new_dfs[i]["p_err"],
            y=(1.0 - (new_dfs[i]["total_nominal_episodes"] / new_dfs[i]["total_n_episodes"]))
            * 100,
            # c=plot_colors[i],
            # marker=markers[i],
        )
    ax.plot(
        np.linspace(new_dfs[0]["p_err"].min(), max_x, 100, endpoint=True),
        np.linspace(new_dfs[0]["p_err"].min(), max_x, 100, endpoint=True),
        "k",
    )

    ax.set(
        title="Threshold Analysis",
        # xlabel=r"$p_\mathrm{err}$",
        ylabel=title_valid_fail_rate,
        ylim=(1e-4, 1e-1),
        yscale="log",
    )
    ax.text(0.001, 0.004, "Single Qubit", rotation=10)

    ax1.set(xlabel=r"$p_\mathrm{err}$", ylabel="%")
    ax1.text(0, 32, "Remaining Syndromes")

    ax1.set_xticks(np.arange(0.0, 0.025, 0.003))
    ax.set_xticks(np.arange(0.0, 0.025, 0.003))

    # plt.legend()
    ax.legend()
    if False:
        plt.savefig("plots/thresh_valid_fail_rate_p_err_log.pdf", bbox_inches="tight")
    plt.show()
