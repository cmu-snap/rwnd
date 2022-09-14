#!/usr/bin/env python

import argparse
import json
import logging
import math
import multiprocessing
import os
from os import path
import pickle
import sys
import time

import matplotlib.pyplot as plt
import numpy as np

from unfair.model import defaults, features, gen_features, utils


# def round_down_to_power_of_2(x):
#     return 2 ** math.floor(np.log2(x))


def get_queue_mult(exp):
    queue_mult = math.floor(exp.queue_bdp)
    if queue_mult == 0:
        return 0.5
    return queue_mult


def plot_cdf(args, disabled, enabled, x_label, x_max, filename):
    count, bins_count = np.histogram(disabled, bins=len(disabled))
    plt.plot(
        bins_count[1:],
        np.cumsum(count / sum(count)),
        alpha=0.75,
        color="r",
        label="Disabled",
    )

    count, bins_count = np.histogram(enabled, bins=len(enabled))
    plt.plot(
        bins_count[1:],
        np.cumsum(count / sum(count)),
        alpha=0.75,
        color="g",
        label="Enabled",
    )

    plt.xlabel(x_label)
    plt.ylabel("CDF")
    plt.xlim(0, x_max)
    plt.title(f"CDF of {x_label}, with and without unfairness monitor")
    plt.legend()
    plt.grid(True)
    plt.tight_layout()

    cdf_flp = path.join(args.out_dir, filename)
    plt.savefig(cdf_flp)
    plt.close()
    logging.info("Saved CDF to: %s", cdf_flp)


def plot_hist(args, disabled, enabled, x_label, filename):
    plt.hist(
        disabled, bins=50, density=True, facecolor="r", alpha=0.75, label="Disabled"
    )
    plt.hist(enabled, bins=50, density=True, facecolor="g", alpha=0.75, label="Enabled")

    plt.xlabel(x_label)
    plt.ylabel("probability (%)")
    plt.title(f"Histogram of {x_label}, with and without unfairness monitor")
    plt.legend()
    plt.grid(True)
    plt.tight_layout()

    hist_flp = path.join(args.out_dir, filename)
    plt.savefig(hist_flp)
    plt.close()
    logging.info("Saved histogram to: %s", hist_flp)


def plot_box(args, data, x_ticks, x_label, y_label, y_max, filename, rotate):
    """
    Make a box plot of the JFI or utilization over some experiment variable like number
    of flows.
    """
    plt.boxplot(data)

    plt.xlabel(x_label)
    plt.ylabel(y_label)
    plt.xticks(
        range(1, len(x_ticks) + 1),
        x_ticks,
        rotation=45 if rotate else 0,
    )
    plt.ylim(0, y_max)
    plt.title(f"Boxplot of {y_label} vs. {x_label}")
    plt.grid(True)
    plt.tight_layout()

    box_flp = path.join(args.out_dir, filename)
    plt.savefig(box_flp)
    plt.close()
    logging.info("Saved boxplot to: %s", box_flp)


def parse_opened_exp(
    exp, exp_flp, exp_dir, out_flp, skip_smoothed, select_tail_percent
):
    # out_flp and skip_smoothed are not used but are kept to maintain API compatibility
    # with gen_features.parse_opened_exp().

    logging.info("Parsing: %s", exp_flp)
    if exp.name.startswith("FAILED"):
        logging.info("Error: Experimant failed: %s", exp_flp)
        return -1
    if exp.tot_flws == 0:
        logging.info("Error: No flows to analyze in: %s", exp_flp)
        return -1

    server_pcap = path.join(exp_dir, f"server-tcpdump-{exp.name}.pcap")
    if not path.exists(server_pcap):
        logging.info("Warning: Missing server pcap file in: %s", exp_flp)
        return -1

    # Determine flow src and dst ports.
    params_flp = path.join(exp_dir, f"{exp.name}.json")
    if not path.exists(params_flp):
        logging.info("Error: Cannot find params file (%s) in: %s", params_flp, exp_flp)
        return -1
    with open(params_flp, "r", encoding="utf-8") as fil:
        params = json.load(fil)

    # Dictionary mapping a flow to its flow's CCA. Each flow is a tuple of the
    # form: (client port, server port)
    #
    # { (client port, server port): CCA }
    flw_to_cca = {
        (client_port, flw[4]): flw[0]
        for flw in params["flowsets"]
        for client_port in flw[3]
    }
    flws = list(flw_to_cca.keys())
    flw_to_pkts = utils.parse_packets(server_pcap, flw_to_cca, select_tail_percent)
    # Discard the ACK packets.
    flw_to_pkts = {flw: data_pkts for flw, (data_pkts, ack_pkts) in flw_to_pkts.items()}
    logging.info("\tParsed packets: %s", server_pcap)
    flw_to_pkts = utils.drop_packets_after_first_flow_finishes(flw_to_pkts)

    late_flows_port = max(flw[4] for flw in params["flowsets"])
    late_flws = [
        flw for flw in flws if flw[1] == late_flows_port and len(flw_to_pkts[flw]) > 0
    ]
    if len(late_flws) == 0:
        logging.info("\tWarning: No late flows to analyze in: %s", exp_flp)
        return exp, -1, -1
    earliest_late_flow_start_time = min(
        [
            flw_to_pkts[flw][features.ARRIVAL_TIME_FET][0]
            for flw in late_flws
            if len(flw_to_pkts[flw]) > 0
        ]
    )

    # Remove data from before the late flows start.
    for flw in flw_to_pkts.keys():
        if len(flw_to_pkts[flw]) == 0:
            flw_to_pkts[flw] = []
            continue
        for idx, arr_time in enumerate(flw_to_pkts[flw][features.ARRIVAL_TIME_FET]):
            if arr_time >= earliest_late_flow_start_time:
                break
        flw_to_pkts[flw] = flw_to_pkts[flw][idx:]

    # zipped_arr_times, zipped_dat = utils.zip_timeseries(
    #     [flw_to_pkts_server[flw][features.ARRIVAL_TIME_FET] for flw in flws],
    #     [flw_to_pkts_server[flw] for flw in flws],
    # )
    # for idx, arr_time in enumerate(zipped_arr_times):
    #     if arr_time >= earliest_late_flow_start_time:
    #         break
    # zipped_arr_times = zipped_arr_times[idx:]
    # zipped_dat = zipped_dat[idx:]

    jfi = get_jfi(flw_to_pkts)
    overall_util = get_avg_util(exp.bw_bps, flw_to_pkts)
    fair_flows_util = get_avg_util(
        exp.bw_bps,
        {flw: pkts for flw, pkts in flw_to_pkts.items() if flw[1] != late_flows_port},
    )
    unfair_flows_util = get_avg_util(
        exp.bw_bps,
        {flw: pkts for flw, pkts in flw_to_pkts.items() if flw[1] == late_flows_port},
    )

    # # Save the results.
    # if path.exists(out_flp):
    #     logging.info(f"\tOutput already exists: {out_flp}")
    # else:
    #     logging.info(f"\tSaving: {out_flp}")
    #     np.savez_compressed(
    #         out_flp,
    #         **{str(k + 1): v for k, v in enumerate(flw_results[flw] for flw in flws)},
    #     )
    return exp, jfi, overall_util, fair_flows_util, unfair_flows_util


def get_jfi(flw_to_pkts):
    flw_to_tput_bps = {
        flw: 0 if len(pkts) == 0 else utils.safe_tput_bps(pkts, 0, len(pkts) - 1)
        for flw, pkts in flw_to_pkts.items()
    }
    return sum(flw_to_tput_bps.values()) ** 2 / (
        len(flw_to_tput_bps) * sum(bits**2 for bits in flw_to_tput_bps.values())
    )


def get_avg_util(bw_bps, flw_to_pkts):
    # Calculate the average combined throughput of all flows by dividing the total bits
    # received by all flows by the time difference between when the first flow started
    # and when the last flow finished.
    bytes_times = (
        (
            utils.safe_sum(pkts[features.WIRELEN_FET], 0, len(pkts) - 1),
            utils.safe_min_win(pkts[features.ARRIVAL_TIME_FET], 0, len(pkts) - 1),
            utils.safe_max_win(pkts[features.ARRIVAL_TIME_FET], 0, len(pkts) - 1),
        )
        for pkts in flw_to_pkts.values()
        if len(pkts) > 0
    )
    byts, start_times_us, end_times_us = zip(*bytes_times)
    avg_total_tput_bps = (
        sum(byts) * 8 / ((max(end_times_us) - min(start_times_us)) / 1e6)
    )
    return avg_total_tput_bps / bw_bps


def group_and_box_plot(
    args,
    matched,
    category_selector,
    output_selector,
    xticks_transformer,
    x_label,
    y_label,
    y_max,
    filename,
    num_buckets,
):

    category_to_values = {
        # Second, extract the value for all the exps in each category.
        xticks_transformer(category): sorted(
            [
                output_selector(matched[exp])
                for exp in matched.keys()
                # Only select experiments for this category.
                if category_selector(exp) == category
            ]
        )
        for category in {
            # First, determine the categories.
            category_selector(exp)
            for exp in matched.keys()
        }
    }
    categories = list(category_to_values.keys())

    # Divide the categories into buckets.
    do_buckets = len(category_to_values) > num_buckets
    if do_buckets:
        min_category = min(categories)
        max_category = max(categories)
        delta = (max_category - min_category) / num_buckets
        category_to_values = {
            f"[{bucket_start:.1f}-{bucket_end:.1f})": [
                # Look through all the categories and grab the values of any category
                # that is in this bucket.
                value
                for category, values in category_to_values.items()
                if (
                    bucket_start
                    <= category
                    < (bucket_end if bucket_end < max_category else math.inf)
                )
                for value in values
            ]
            for bucket_start, bucket_end in [
                # Define the start and end of each bucket.
                (min_category + delta * i, min_category + delta * (i + 1))
                for i in range(num_buckets)
            ]
        }

    # Get a list of the categories, and a list of lists of the category values.
    categories, values = zip(
        *sorted(
            category_to_values.items(),
            key=lambda x: float(x[0].split("-")[0].strip("[")) if do_buckets else x[0],
        )
    )

    plot_box(
        args, values, categories, x_label, y_label, y_max, filename, rotate=do_buckets
    )


def main(args):
    log_flp = path.join(args.out_dir, "output.log")
    logging.basicConfig(
        filename=log_flp,
        filemode="w",
        format="%(asctime)s %(levelname)s %(message)s",
        level=logging.DEBUG,
    )
    print("Logging to:", log_flp)
    logging.info("Evaluating experiments in: %s", args.exp_dir)

    # Find all experiments.
    pcaps = [
        (
            path.join(args.exp_dir, exp),
            args.untar_dir,
            args.out_dir,
            False,
            args.select_tail_percent,
            parse_opened_exp,
        )
        for exp in sorted(os.listdir(args.exp_dir))
        if exp.endswith(".tar.gz")
    ]

    logging.info("Num files: %d", len(pcaps))
    start_time_s = time.time()

    data_flp = path.join(args.out_dir, "results.pickle")
    if path.exists(data_flp):
        logging.info("Loading data from: %s", data_flp)
        # Load existing raw JFI results.
        with open(data_flp, "rb") as fil:
            results = pickle.load(fil)
        if len(results) != len(pcaps):
            logging.info(
                (
                    "Error: Expected %d JFI results, but found %d. "
                    "Delete %s and try again."
                ),
                len(pcaps),
                len(results),
                data_flp,
            )
            return 1
    else:
        if defaults.SYNC:
            results = {gen_features.parse_exp(*pcap) for pcap in pcaps}
        else:
            with multiprocessing.Pool(processes=args.parallel) as pol:
                results = set(pol.starmap(gen_features.parse_exp, pcaps))
        # Save raw JFI results from parsed experiments.
        with open(data_flp, "wb") as fil:
            pickle.dump(results, fil)

    # Dict mapping experiment to JFI.
    results = {
        exp_results[0]: tuple(exp_results[1:])
        for exp_results in results
        if (isinstance(exp_results, tuple) and -1 not in exp_results[1:])
    }
    # Experiments in which the unfairness monitor was enabled.
    enabled = {exp for exp in results.keys() if exp.use_unfairness_monitor}
    # Experiments in which the unfairness monitor was disabled.
    disabled = {exp for exp in results.keys() if not exp.use_unfairness_monitor}

    # Match each enabled experiment with its corresponding disabled experiment and
    # compute the JFI delta. matched is a dict mapping the name of the enabled
    # experiment to a tuple of the form:
    #     ( disabled JFI, enabled JFI, difference in JFI from enabled to disabled )
    matched = {}
    for enabled_exp in enabled:
        # Find the corresponding experiment with the unfairness monitor disabled.
        target_disabled_name = enabled_exp.name.replace("unfairTrue", "unfairFalse")
        target_disabled_exp = None
        for disabled_exp in disabled:
            if disabled_exp.name == target_disabled_name:
                target_disabled_exp = disabled_exp
                break
        if target_disabled_exp is None:
            logging.info(
                "Warning: Cannot find experiment with unfairness monitor disabled: %s",
                target_disabled_name,
            )
            continue

        (
            jfi_disabled,
            overall_util_disabled,
            fair_flows_util_disabled,
            unfair_flows_util_disabled,
        ) = results[target_disabled_exp]
        (
            jfi_enabled,
            overall_util_enabled,
            fair_flows_util_enabled,
            unfair_flows_util_enabled,
        ) = results[enabled_exp]

        matched[enabled_exp] = (
            jfi_disabled,  # 0
            jfi_enabled,  # 1
            jfi_enabled - jfi_disabled,  # 2
            (jfi_enabled - jfi_disabled) / jfi_disabled * 100,  # 3
            overall_util_disabled * 100,  # 4
            overall_util_enabled * 100,  # 5
            (overall_util_enabled - overall_util_disabled) * 100,  # 6
            fair_flows_util_disabled * 100,  # 7
            fair_flows_util_enabled * 100,  # 8
            (fair_flows_util_enabled - fair_flows_util_disabled) * 100,  # 9
            unfair_flows_util_disabled * 100,  # 10
            unfair_flows_util_enabled * 100,  # 11
            (unfair_flows_util_enabled - unfair_flows_util_disabled) * 100,  # 12
        )
    # Save JFI results.
    with open(path.join(args.out_dir, "results.json"), "w", encoding="utf-8") as fil:
        json.dump({exp.name: val for exp, val in matched.items()}, fil, indent=4)

    logging.info("Matched experiments: %d", len(matched))
    (
        jfis_disabled,
        jfis_enabled,
        _,
        jfi_deltas_percent,
        overall_utils_disabled,
        overall_utils_enabled,
        overall_util_deltas_percent,
        fair_flows_utils_disabled,
        fair_flows_utils_enabled,
        fair_flows_util_deltas_percent,
        unfair_flows_utils_disabled,
        unfair_flows_utils_enabled,
        unfair_flows_util_deltas_percent,
    ) = list(zip(*matched.values()))

    plot_hist(args, jfis_disabled, jfis_enabled, "JFI", "jfi_hist.pdf")
    plot_hist(
        args,
        overall_utils_disabled,
        overall_utils_enabled,
        "overall link utilization (%)",
        "overall_util_hist.pdf",
    )
    plot_hist(
        args,
        fair_flows_utils_disabled,
        fair_flows_utils_enabled,
        '"fair" flows link utilization (%)',
        "fair_flows_util_hist.pdf",
    )
    plot_hist(
        args,
        unfair_flows_utils_disabled,
        unfair_flows_utils_enabled,
        '"unfair" flows link utilization (%)',
        "unfair_flows_util_hist.pdf",
    )
    plot_cdf(
        args,
        # [1 - x for x in jfis_disabled],
        # [1 - x for x in jfis_enabled],
        jfis_disabled,
        jfis_enabled,
        "JFI",
        1.0,
        "jfi_cdf.pdf",
    )
    plot_cdf(
        args,
        [100 - x for x in overall_utils_disabled],
        [100 - x for x in overall_utils_enabled],
        "unused link capacity (%)",
        100,
        "unused_util_cdf.pdf",
    )
    plot_cdf(
        args,
        overall_utils_disabled,
        overall_utils_enabled,
        "overall link utilization (%)",
        100,
        "util_cdf.pdf",
    )
    plot_cdf(
        args,
        fair_flows_utils_disabled,
        fair_flows_utils_enabled,
        '"fair" flows link utilization (%)',
        100,
        "fair_flows_util_cdf.pdf",
    )
    plot_cdf(
        args,
        unfair_flows_utils_disabled,
        unfair_flows_utils_enabled,
        '"unfair" flows link utilization (%)',
        100,
        "unfair_flows_util_cdf.pdf",
    )

    logging.info(
        (
            "\nOverall JFI change (percent) --- higher is better:\n"
            "\tAvg: %s%.4f %%\n"
            "\tStddev: %.4f %%\n"
            "\tVar: %.4f %%"
        ),
        "+" if np.mean(jfi_deltas_percent) > 0 else "",
        np.mean(jfi_deltas_percent),
        np.std(jfi_deltas_percent),
        np.var(jfi_deltas_percent),
    )
    logging.info(
        "Overall average JFI with monitor enabled: %.4f", np.mean(jfis_enabled)
    )
    logging.info(
        (
            "\nOverall link utilization change "
            "--- higher is better, want to be >= 0%%:\n"
            "\tAvg: %s%.4f %%\n"
            "\tStddev: %.4f %%\n"
            "\tVar: %.4f %%"
        ),
        "+" if np.mean(overall_util_deltas_percent) > 0 else "",
        np.mean(overall_util_deltas_percent),
        np.std(overall_util_deltas_percent),
        np.var(overall_util_deltas_percent),
    )
    logging.info(
        "Overall average link utilization with monitor enabled: %.4f %%",
        np.mean(overall_utils_enabled),
    )
    logging.info(
        (
            '\n"Fair" flows link utilization change '
            "--- higher is better, want to be >= 0%%:\n"
            "\tAvg: %s%.4f %%\n"
            "\tStddev: %.4f %%\n"
            "\tVar: %.4f %%"
        ),
        "+" if np.mean(fair_flows_util_deltas_percent) > 0 else "",
        np.mean(fair_flows_util_deltas_percent),
        np.std(fair_flows_util_deltas_percent),
        np.var(fair_flows_util_deltas_percent),
    )
    logging.info(
        (
            '\n"Unfair" flows link utilization change '
            "--- higher is better, want to be >= 0%%:\n"
            "\tAvg: %s%.4f %%\n"
            "\tStddev: %.4f %%\n"
            "\tVar: %.4f %%"
        ),
        "+" if np.mean(unfair_flows_util_deltas_percent) > 0 else "",
        np.mean(unfair_flows_util_deltas_percent),
        np.std(unfair_flows_util_deltas_percent),
        np.var(unfair_flows_util_deltas_percent),
    )

    # Break down utilization based on experiment parameters.
    group_and_box_plot(
        args,
        matched,
        lambda exp: exp.bw_bps,
        lambda result: result[5],
        lambda x: int(x / 1e6),
        "Bandwidth (Mbps)",
        "Utilization (%)",
        100,
        "bandwidth_vs_util.pdf",
        num_buckets=10,
    )
    group_and_box_plot(
        args,
        matched,
        lambda exp: exp.rtt_us,
        lambda result: result[5],
        lambda x: int(x / 1e3),
        "RTT (ms)",
        "Utilization (%)",
        100,
        "rtt_vs_util.pdf",
        num_buckets=10,
    )
    group_and_box_plot(
        args,
        matched,
        get_queue_mult,
        lambda result: result[5],
        lambda x: x,
        "Queue size (x BDP)",
        "Utilization (%)",
        100,
        "queue_size_vs_util.pdf",
        num_buckets=10,
    )
    group_and_box_plot(
        args,
        matched,
        lambda exp: exp.cca_1_flws,
        lambda result: result[5],
        lambda x: x,
        "Incumbent flows",
        "Utilization (%)",
        100,
        "incumbent_flows_vs_util.pdf",
        num_buckets=10,
    )

    # Break down JFI based on experiment parameters.
    group_and_box_plot(
        args,
        matched,
        lambda exp: exp.bw_bps,
        lambda result: result[1],
        lambda x: int(x / 1e6),
        "Bandwidth (Mbps)",
        "JFI",
        1,
        "bandwidth_vs_jfi.pdf",
        num_buckets=10,
    )
    group_and_box_plot(
        args,
        matched,
        lambda exp: exp.rtt_us,
        lambda result: result[1],
        lambda x: int(x / 1e3),
        "RTT (ms)",
        "JFI",
        1,
        "rtt_vs_jfi.pdf",
        num_buckets=10,
    )
    group_and_box_plot(
        args,
        matched,
        get_queue_mult,
        lambda result: result[1],
        lambda x: x,
        "Queue size (x BDP)",
        "JFI",
        1,
        "queue_size_vs_jfi.pdf",
        num_buckets=10,
    )
    group_and_box_plot(
        args,
        matched,
        lambda exp: exp.cca_1_flws,
        lambda result: result[1],
        lambda x: x,
        "Incumbent flows",
        "JFI",
        1,
        "incumbent_flows_vs_jfi.pdf",
        num_buckets=10,
    )

    logging.info("Done analyzing - time: %.2f seconds", time.time() - start_time_s)
    return 0


def parse_args():
    parser = argparse.ArgumentParser(description="Evaluation.")
    parser.add_argument(
        "--exp-dir",
        help="The directory in which the experiment results are stored.",
        required=True,
        type=str,
    )
    parser.add_argument(
        "--untar-dir",
        help=(
            "The directory in which the untarred experiment intermediate "
            "files are stored (required)."
        ),
        required=True,
        type=str,
    )
    parser.add_argument(
        "--parallel",
        default=multiprocessing.cpu_count(),
        help="The number of files to parse in parallel.",
        type=int,
    )
    parser.add_argument(
        "--out-dir",
        help="The directory in which to store the results.",
        required=True,
        type=str,
    )
    parser.add_argument(
        "--select-tail-percent",
        help="The percentage (by time) of the tail of the PCAPs to select.",
        required=False,
        type=float,
    )
    args = parser.parse_args()
    assert path.isdir(args.exp_dir)
    assert path.isdir(args.out_dir)
    return args


if __name__ == "__main__":
    sys.exit(main(parse_args()))
