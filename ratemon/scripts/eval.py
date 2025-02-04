#!/usr/bin/env python

import argparse
import collections
import json
import logging
import math
import multiprocessing
import os
import pickle
import random
import sys
import time
from os import path

import matplotlib.pyplot as plt
import numpy as np

from ratemon.model import defaults, features, gen_features, utils

FIGSIZE = (5, 2.2)
FIGSIZE_BOX = (5, 3.5)
FIGSIZE_BAR = (5, 2.5)
FONTSIZE = 12
# COLORS = ["b", "r", "g"]
COLORS_MAP = {
    "red": "#d7191c",
    "blue": "#2c7bb6",
    "orange": "#fdae61",
}
COLORS = [COLORS_MAP["red"], COLORS_MAP["blue"], COLORS_MAP["orange"]]
LINESTYLES = ["solid", "dashed", "dashdot"]
LINEWIDTH = 2.5
PREFIX = ""
PERCENTILES = [5, 10, 25, 50, 75, 90, 99.9]
# Bucket size for computing average throughput. To avoid showing transient burstiness,
# this should be larger than the largest possible RTT.
BUCKET_DUR_US = 1e6


def get_queue_mult(exp, vals):
    queue_mult = math.floor(exp.queue_bdp)
    if queue_mult == 0:
        return 0.5
    return queue_mult


def plot_cdf(
    args,
    lines,
    labels,
    x_label,
    x_max,
    filename,
    title=None,
    colors=COLORS,
    linestyles=LINESTYLES,
    legendloc="best",
):
    plt.figure(figsize=FIGSIZE)
    plt.grid(True)

    for line, label, color, linestyle in zip(lines, labels, colors, linestyles):
        count, bins_count = np.histogram(line, bins=len(line))
        plt.plot(
            bins_count[1:],
            np.cumsum(count / sum(count)),
            alpha=0.75,
            color=color,
            linestyle=linestyle,
            label=label,
            linewidth=LINEWIDTH,
        )

    plt.xlabel(x_label, fontsize=FONTSIZE)
    plt.ylabel("CDF", fontsize=FONTSIZE)
    plt.xlim(0, x_max)
    if title is not None:
        plt.title(title, fontsize=FONTSIZE)
    if len(lines) > 1:
        plt.legend(fontsize=FONTSIZE, loc=legendloc)

    cdf_flp = path.join(args.out_dir, PREFIX + filename)
    plt.tight_layout()
    plt.savefig(cdf_flp)
    plt.close()
    logging.info("Saved CDF to: %s", cdf_flp)

    with open(
        path.join(args.out_dir, PREFIX + filename[:-4] + "_percentiles.txt"),
        "w",
        encoding="utf-8",
    ) as fil:
        for line, label in zip(lines, labels):
            fil.write(
                f"Percentiles for {label}: "
                f"{dict(zip(PERCENTILES, np.percentile(line, PERCENTILES)))}\n"
            )


def plot_hist(args, lines, labels, x_label, filename, title=None, colors=COLORS):
    plt.figure(figsize=FIGSIZE)

    for line, label, color in zip(lines, labels, colors):
        plt.hist(line, bins=50, density=True, facecolor=color, alpha=0.75, label=label)

    plt.xlabel(x_label, fontsize=FONTSIZE)
    plt.ylabel("probability (%)", fontsize=FONTSIZE)
    if title is not None:
        plt.title(title, fontsize=FONTSIZE)
    if len(lines) > 1:
        plt.legend(fontsize=FONTSIZE)
    plt.grid(True)

    hist_flp = path.join(args.out_dir, PREFIX + filename)
    plt.tight_layout()
    plt.savefig(hist_flp)
    plt.close()
    logging.info("Saved histogram to: %s", hist_flp)


def plot_box(
    args, data, x_ticks, x_label, y_label, y_max, filename, rotate, title=None
):
    """
    Make a box plot of the JFI or utilization over some experiment variable like
    number of flows.
    """
    plt.figure(figsize=FIGSIZE_BOX)
    plt.grid(True)

    plt.boxplot(data)

    plt.xlabel(x_label, fontsize=FONTSIZE)
    plt.ylabel(y_label, fontsize=FONTSIZE)
    plt.xticks(
        list(range(1, len(x_ticks) + 1)),
        x_ticks,
        rotation=45 if rotate else 0,
    )
    plt.ylim(0, y_max)
    if title is not None:
        plt.title(title, fontsize=FONTSIZE)

    box_flp = path.join(args.out_dir, PREFIX + filename)
    plt.tight_layout()
    plt.savefig(box_flp)
    plt.close()
    logging.info("Saved boxplot to: %s", box_flp)


def plot_lines(
    lines,
    x_label,
    y_label,
    x_max,
    y_max,
    out_flp,
    legendloc="best",
    linewidth=1,
    colors=None,
    bbox_to_anchor=None,
    legend_ncol=1,
    figsize=FIGSIZE,
):
    """An element in lines is a tuple of the form: ( label, ( xs, ys ) )"""
    plt.figure(figsize=figsize)
    plt.grid(True)

    for idx, line in enumerate(lines):
        line, label = line
        if len(line) > 0:
            xs, ys = zip(*line)
            plt.plot(
                xs,
                ys,
                alpha=0.75,
                linestyle=(
                    # If this is a servicepolicy graph but not the first
                    # sender, or a cubic flow in a flow fairness graph...
                    "solid"
                    if "Service 2" in label or label == "cubic"
                    else "dashdot"
                ),
                label=label,
                linewidth=linewidth,
                **{} if colors is None else {"color": colors[idx]},
            )

    plt.xlabel(x_label, fontsize=FONTSIZE)
    plt.ylabel(y_label, fontsize=FONTSIZE)
    if x_max is not None:
        plt.xlim(0, x_max)
    plt.ylim(0, y_max)
    plt.legend(
        loc=legendloc,
        fontsize=FONTSIZE,
        ncol=legend_ncol,
        **({} if bbox_to_anchor is None else {"bbox_to_anchor": bbox_to_anchor}),
    )

    plt.tight_layout()
    plt.savefig(out_flp, bbox_inches="tight")
    plt.close()
    logging.info("Saved line graph to: %s", out_flp)


def plot_flows_over_time(
    exp,
    out_flp,
    flw_to_pkts,
    flw_to_cca,
    servicepolicy=False,
    flw_to_sender=None,
    xlim=None,
    bottleneck_Mbps=None,
):
    lines = []
    initial_time = min(
        np.min(pkts[features.ARRIVAL_TIME_FET]) for pkts in flw_to_pkts.values()
    )
    # Put packets into buckets of BUCKET_DUR_US.
    for flw, pkts in flw_to_pkts.items():
        throughputs = []
        current_bucket = []
        for idx, pkt in enumerate(pkts):
            if not current_bucket:
                current_bucket = [idx]
                continue

            start_idx = current_bucket[0]
            start_time = pkts[start_idx][features.ARRIVAL_TIME_FET]
            # Create a bucket for every BUCKET_DUR_US.
            if (
                len(current_bucket) > 1
                and pkt[features.ARRIVAL_TIME_FET] - start_time > BUCKET_DUR_US
            ):
                # End this bucket. Calculate the bucket's throughput and create a new bucket.
                end_idx = current_bucket[-1]
                end_time = pkts[end_idx][features.ARRIVAL_TIME_FET]
                # print("start:", start_idx)
                # print("end:", end_idx)
                # print("start_time:", start_time)
                # print("end_time:", end_time)
                # print("end_time - start_time:", end_time - start_time)
                throughputs.append(
                    (
                        (start_time + (end_time - start_time) / 2 - initial_time) / 1e6,
                        utils.safe_tput_bps(pkts, start_idx, end_idx) / 1e6,
                    )
                )
                # print(throughputs[-1])
                current_bucket = []
            current_bucket.append(idx)

        # Skips the last partial bucket, but that's okay.

        lines.append((throughputs, flw))

    # If servicepolicy, then graph the total throughput of each sender instead of the
    # throughput of each flow.
    if servicepolicy and flw_to_sender is not None:
        sender_to_tputs = dict()
        # Accumulate the throughput of each sender.
        for throughputs, flw in lines:
            sender = flw_to_sender[flw]
            if sender not in sender_to_tputs:
                sender_to_tputs[sender] = [
                    flw_to_cca[flw],
                    0,
                    [[time_s, 0] for time_s, _ in throughputs],
                ]
            # Make sure that all flows from this sender use the same CCA.
            if sender_to_tputs[sender][0] != flw_to_cca[flw]:
                logging.error(
                    "Sender %s has multiple CCAs: %s, %s",
                    sender,
                    sender_to_tputs[sender][0],
                    flw_to_cca[flw],
                )
                continue
            sender_to_tputs[sender][1] += 1
            for sample_idx, (_, tput_Mbps) in enumerate(throughputs):
                if sample_idx < len(sender_to_tputs[sender][2]):
                    sender_to_tputs[sender][2][sample_idx][1] += tput_Mbps
                else:
                    break
                # except:
                #     print("len(sender_to_tputs[sender])", len(sender_to_tputs[sender]))
                #     print("len(sender_to_tputs[sender][2])", len(sender_to_tputs[sender][2]))
                #     print("len(sender_to_tputs[sender][2][0])", len(sender_to_tputs[sender][2][0]))
                #     print("len(throughputs)", len(throughputs))
                #     print("sample_idx", sample_idx)
                #     raise
        lines = [
            (throughputs, f"Service {sender_idx + 1}: {num_flows} {cca}")
            for sender_idx, (cca, num_flows, throughputs) in enumerate(
                sender_to_tputs.values()
            )
        ]
    else:
        lines = [(throughputs, flw_to_cca[flw]) for (throughputs, flw) in lines]

    colors = [COLORS_MAP["blue"], COLORS_MAP["red"]] if servicepolicy else None

    # If we are supposed to mark the bottleneck bandwidth, then create a horizontal
    # line and prepend it to the lines.
    if bottleneck_Mbps is not None:
        start_time_s = min(points[0][0] for points, _ in lines)
        end_time_s = max(points[-1][0] for points, _ in lines)
        lines.insert(
            0,
            (
                [(start_time_s, bottleneck_Mbps), (end_time_s, bottleneck_Mbps)],
                "Bottleneck",
            ),
        )
        # Make the line orange.
        if colors is not None:
            colors.insert(0, COLORS_MAP["orange"])

    plot_lines(
        lines,
        "time (s)",
        "throughput (Mbps)",
        None,
        exp.bw_Mbps if exp.use_bess else None,
        out_flp,
        legendloc=("center" if servicepolicy else "upper right"),
        linewidth=(1 if servicepolicy else 1),
        colors=colors,
        bbox_to_anchor=((0.5, 1.15) if servicepolicy else None),
        legend_ncol=(2 if servicepolicy else 1),
        figsize=(5, 2.6),
    )


def plot_bar(
    args,
    lines,
    labels,
    x_label,
    y_label,
    x_tick_labels,
    filename,
    rotate=None,
    y_max=None,
    title=None,
    colors=COLORS,
    legendloc="best",
    stacked=False,
    legend_ncol=1,
):
    bar_count = len(lines)
    assert bar_count <= 2
    if stacked:
        assert bar_count == 2
        bar_count = 1

    plt.figure(figsize=FIGSIZE_BAR)
    # plt.grid(True)

    width = 0.75
    count = len(lines[0])
    bar_xs = list(range(bar_count, bar_count * (count + 1), bar_count))
    label_xs = [x + (width / 2 * bar_count) for x in bar_xs]

    for line_idx, (line, label, color) in enumerate(zip(lines, labels, colors)):
        plt.bar(
            (
                bar_xs
                if bar_count == 1
                else [x + (-1 if line_idx == 0 else 1) * (width / 2) for x in bar_xs]
            ),
            (
                [val - lines[line_idx - 1][val_idx] for val_idx, val in enumerate(line)]
                if stacked and line_idx == 1
                else line
            ),
            alpha=0.75,
            width=width,
            color=color,
            align="center",
            label=label,
            **(
                {"bottom": lines[line_idx - 1] if line_idx == 1 else 0}
                if stacked
                else {}
            ),
            **({"hatch": "////"} if stacked and line_idx == 1 else {}),
        )

    plt.xticks(
        ticks=label_xs,
        labels=x_tick_labels,
        fontsize=FONTSIZE,
        rotation=45 if rotate else 0,
        ha="right" if rotate else "center",
    )
    plt.tick_params(axis="x", length=0)
    plt.xlabel(x_label, fontsize=FONTSIZE)
    plt.ylabel(y_label, fontsize=FONTSIZE)
    plt.xlim(0, max(bar_xs) + 1)
    plt.ylim(min(0, lines[0]), y_max)
    if title is not None:
        plt.title(title, fontsize=FONTSIZE)
    if labels[0] is not None:
        plt.legend(loc=legendloc, ncol=legend_ncol, fontsize=FONTSIZE)

    bar_flp = path.join(args.out_dir, PREFIX + filename)
    plt.tight_layout()
    plt.savefig(bar_flp)
    plt.close()
    logging.info("Saved bar graph to: %s", bar_flp)


def parse_opened_exp(
    exp,
    exp_flp,
    exp_dir,
    out_flp,
    skip_smoothed,
    select_tail_percent,
    servicepolicy,
):
    # skip_smoothed is not used but is kept to maintain API compatibility
    # with gen_features.parse_opened_exp().

    logging.info("Parsing: %s", exp_flp)

    # Load results if they already exist.
    if path.exists(out_flp):
        logging.info("Found results: %s", out_flp)
        try:
            with open(out_flp, "rb") as fil:
                out = pickle.load(fil)
                assert len(out) == 5 and isinstance(
                    out[0], utils.Exp
                ), f"Improperly formatted results file: {out_flp}"
                return out
        except FileNotFoundError:
            logging.exception("Cannot find results in: %s", out_flp)
        except pickle.PickleError:
            logging.exception("Pickle error when loads results from: %s", out_flp)
        except AssertionError:
            logging.exception("Improperly formatted results file: %s", out_flp)
    # Check for basic errors.
    if exp.name.startswith("FAILED"):
        logging.info("Error: Experimant failed: %s", exp_flp)
        return -1
    if exp.tot_flws == 0:
        logging.info("Error: No flows to analyze in: %s", exp_flp)
        return -1

    params = get_params(exp_dir)
    category = params["category"]

    # Dictionary mapping a flow to its flow's CCA. Each flow is a tuple of the
    # form: (sender port, receiver port)
    #
    # { (sender port, receiver port): CCA }
    flw_to_cca = {
        (sender_port, flw[6]): flw[2]
        for flw in params["flowsets"]
        for sender_port in flw[5]
    }
    flws = list(flw_to_cca.keys())
    # Map flow to sender IP address (LAN). Each flow tuple will be unique because
    # the receiver ports are unique across flows from different senders.
    flw_to_sender = {
        (sender_port, flw[6]): flw[0][0]
        for flw in params["flowsets"]
        for sender_port in flw[5]
    }
    sender_to_flws = collections.defaultdict(list)
    for flw, sender in flw_to_sender.items():
        sender_to_flws[sender].append(flw)

    receiver_name_to_ip = {flw[1][0]: flw[1][7] for flw in params["flowsets"]}
    assert receiver_name_to_ip, "Cannot determine receiver(s)."

    # Need to process all PCAPs to build a combined record of all flows.
    flw_to_pkts = dict()
    for receiver_name, receiver_ip in receiver_name_to_ip.items():
        receiver_pcap = path.join(exp_dir, f"{receiver_name}-tcpdump-{exp.name}.pcap")

        if not path.exists(receiver_pcap):
            logging.error(
                "Error: Missing pcap file in: %s --- %s", exp_flp, receiver_pcap
            )
            return -1

        for flw, pkts in utils.parse_packets(
            receiver_pcap, flw_to_cca, receiver_ip, select_tail_percent
        ).items():
            if sum(len(p) for p in pkts) > 0:
                # Only add flow if parse_packets() found at least one packet. This is
                # to support multiple receivers, where parse_packets() checks each
                # receiver for all flows even if each receiver only has a subset of
                # flows.
                flw_to_pkts[flw] = pkts

        logging.info("\tParsed packets: %s", receiver_pcap)

    # Discard the ACK packets.
    flw_to_pkts = {flw: data_pkts for flw, (data_pkts, ack_pkts) in flw_to_pkts.items()}

    # Normalize the packet arrival times to the start of the experiment.
    earliest_start_time_us = min(
        pkts[features.ARRIVAL_TIME_FET][0] for pkts in flw_to_pkts.values()
    )
    for flw in flw_to_pkts:
        flw_to_pkts[flw][features.ARRIVAL_TIME_FET] -= earliest_start_time_us

    # Plot flows over time.
    plot_flows_over_time(
        exp,
        out_flp[:-4] + "_flows.pdf",
        flw_to_pkts,
        flw_to_cca,
        servicepolicy,
        flw_to_sender,
    )
    # Plot each sender separately.
    for sender, flws in sender_to_flws.items():
        plot_flows_over_time(
            exp,
            out_flp[:-4] + f"_flows-from-{sender}.pdf",
            {flw: flw_to_pkts[flw] for flw in flws},
            flw_to_cca,
        )

    # Drop packets from before the last flow starts and after the first flow ends.
    latest_start_time_us = max(
        pkts[features.ARRIVAL_TIME_FET][0] for pkts in flw_to_pkts.values()
    )
    earliest_end_time_us = min(
        pkts[features.ARRIVAL_TIME_FET][-1] for pkts in flw_to_pkts.values()
    )
    flw_to_pkts = utils.trim_packets(
        flw_to_pkts, latest_start_time_us, earliest_end_time_us
    )

    overall_util = 0
    # Flow class to overall utilization of that flow class.
    class_to_util = {}
    # Bottleneck time range to average ratio of flow throughput to maxmin fair rate.
    bneck_to_maxmin_ratios = None
    if exp.use_bess:
        overall_util = get_avg_util(exp.bw_bps, flw_to_pkts)

        # Calculate class-based utilization numbers.
        # Determine a mapping from class to flows in that class.
        class_to_flws = collections.defaultdict(list)
        classifier = CLASSIFIERS[CATEGORIES[category][0]]
        for flw in params["flowsets"]:
            flow_class = classifier(flw)
            for sender_port in flw[5]:
                class_to_flws[flow_class].append((sender_port, flw[6]))
        # Calculate the utilization of each class.
        class_to_util = {
            flow_class: get_avg_util(
                exp.bw_bps,
                {flw: flw_to_pkts[flw] for flw in flws},
            )
            for flow_class, flws in class_to_flws.items()
        }

        # Specific analysis for multibottleneck experiments.
        if category == "multibottleneck":
            bneck_to_maxmin_ratios = calculate_maxmin_ratios(
                params, flw_to_pkts, flw_to_sender, sender_to_flws
            )

    out = (
        exp,
        params,
        get_jfi(flw_to_pkts, servicepolicy, flw_to_sender),
        overall_util,
        class_to_util,
        bneck_to_maxmin_ratios,
    )

    # Save the results.
    logging.info("\tSaving: %s", out_flp)
    with open(out_flp, "wb") as fil:
        pickle.dump(out, fil)

    return out


def calculate_maxmin_ratios(params, flw_to_pkts, flw_to_sender, sender_to_flws):
    # For each bottleneck situation
    #     For each flow
    #         Determine maxmin-fair rate
    #         Determine actual rate
    #         Calculate ratio
    #     Average the ratios
    # Return array with one average ratio per bottleneck situation

    # Add all bottleneck events to unified list. Replace rates of 0
    # (no bottleneck) with the BESS bandwidth.
    # Dict mapping time to a list of bottleneck events at that time.
    startsec_to_sender_to_ratebps = collections.defaultdict(dict)
    for sender, bneck_schedule in params["sender_bottlenecks"].items():
        for bneck_event in bneck_schedule:
            rate_bps = bneck_event["rate_Mbps"] * 1e6
            startsec_to_sender_to_ratebps[bneck_event["time_s"]][sender] = (
                float("inf") if rate_bps == 0 else rate_bps
            )
    # Go through the list of events and populate the rate values of any senders
    # that did not change.
    start_times_s = sorted(startsec_to_sender_to_ratebps.keys())
    for idx, start_s in enumerate(start_times_s[:-1]):
        for sender, rate_bps in startsec_to_sender_to_ratebps[start_s].items():
            next_start_s = start_times_s[idx + 1]
            if sender not in startsec_to_sender_to_ratebps[next_start_s]:
                startsec_to_sender_to_ratebps[next_start_s][sender] = rate_bps
    # Sort bottleneck events by time.
    startsec_to_sender_to_ratebps_sorted = sorted(
        startsec_to_sender_to_ratebps.items(), key=lambda x: x[0]
    )

    # Create bottleneck situations. A bottleneck situation is a time range with a set
    # of sender rates.
    end_s = max(flowset[4] for flowset in params["flowsets"])
    bneck_situations = []
    for idx, bneck in enumerate(startsec_to_sender_to_ratebps_sorted):
        start_s, sender_to_ratebps = bneck
        next_start_s = (
            # If this is the last bottleneck situation, then set the end time to the
            # experiment end time.
            end_s
            if idx == len(startsec_to_sender_to_ratebps_sorted) - 1
            # Otherwise (this is NOT the last bottleneck situation), set the end time to
            # the start time of the next bottleneck situation.
            else startsec_to_sender_to_ratebps_sorted[idx + 1][0]
        )
        bneck_situations.append((start_s, next_start_s, sender_to_ratebps))
    assert (
        len(bneck_situations) == 3
    ), f"Error: Expected 3 bottleneck situations, but found: {len(bneck_situations)}"

    # Determine the maxmin fair rate for each flow in each bottleneck situation
    bneck_to_sender_to_maxminbps = collections.defaultdict(dict)
    for start_s, end_s, sender_to_ratebps in bneck_situations:
        bneck = (start_s, end_s)
        # Calculate the maxmin fair rate at the sender bottlenecks.
        for sender, flws in sender_to_flws.items():
            bneck_to_sender_to_maxminbps[bneck][sender] = sender_to_ratebps[
                sender
            ] / len(flws)

        # Calculate the maxmin fair rate including the shared bottleneck. Remember, a
        # flow only has one maxmin fair rate. So we override the existing values in
        # bneck_to_sender_to_maxminbps.

        # Assume there are only two senders. Then we have three cases:
        #   1) No sender bottlenecks. Maxmin fair rate is entirely determined by the
        #      shared bottleneck. Flows divide the shared bottleneck equally.
        #   2) One sender bottleneck. Subtract from the shared bottleneck the rate of
        #      the sender with the bottleneck. Then divide the remainder equally
        #      between the other sender's flows.
        #   3) Two sender bottlenecks. Do nothing, as the maxmin rates are already set
        #      based on the sender bottlenecks and the shared bottleneck does not
        #      matter.
        #      Note: Case 3 assumes that the sender bottlenecks together are less than
        #            the shared bottleneck.

        assert (
            len(bneck_to_sender_to_maxminbps[bneck]) == 2
        ), "Error: Must be exactly two senders!"
        senders, maxmin_rates_bps = zip(*bneck_to_sender_to_maxminbps[bneck].items())

        # Look up the sender with the smaller maxmin rate.
        smaller_idx = np.argmin(maxmin_rates_bps)
        smaller_sender = senders[smaller_idx]
        # Note that this is per-flow.
        smaller_maxmin_rate_bps = maxmin_rates_bps[smaller_idx]

        # Look up the sender with the larger maxmin rate.
        larger_idx = (
            {0, 1}
            - {
                smaller_idx,
            }
        ).pop()
        larger_sender = senders[larger_idx]
        # Note that this is per-flow.
        larger_maxmin_rate_bps = maxmin_rates_bps[larger_idx]

        assert smaller_idx != larger_idx
        assert tuple(sorted([smaller_idx, larger_idx])) == (0, 1)

        # Case 1 above.
        if smaller_maxmin_rate_bps == float("inf"):
            rate_bps = (
                params["bess_bw_Mbps"]
                / (
                    len(sender_to_flws[smaller_sender])
                    + len(sender_to_flws[larger_sender])
                )
                * 1e6
            )
            bneck_to_sender_to_maxminbps[bneck][smaller_sender] = rate_bps
            bneck_to_sender_to_maxminbps[bneck][larger_sender] = rate_bps
        # Case 2 above.
        elif (smaller_maxmin_rate_bps != float("inf")) and (
            larger_maxmin_rate_bps == float("inf")
        ):
            remainder = params["bess_bw_Mbps"] * 1e6 - sender_to_ratebps[smaller_sender]
            bneck_to_sender_to_maxminbps[bneck][larger_sender] = remainder / len(
                sender_to_flws[larger_sender]
            )
        # Case 3 above.
        else:
            total_sender_bneck_bps = (
                sender_to_ratebps[smaller_sender] + sender_to_ratebps[larger_sender]
            )
            assert total_sender_bneck_bps < (params["bess_bw_Mbps"] * 1e6), (
                f"Error: Sum of the sender bottlenecks ({total_sender_bneck_bps} bps) "
                "must be less than the shared bottleneck "
                f"({params['bess_bw_Mbps'] * 1e6} bps)!"
            )

    # For each bottleneck situation, compare each flow's actual throughput to its
    # maxmin fair rate.
    flw_to_last_cutoff_idx = collections.defaultdict(int)
    bneck_to_maxmin_ratios = {}
    for start_s, end_s, _ in bneck_situations:
        bneck = (start_s, end_s)
        flw_to_maxmin_ratio = {}
        for flw, pkts in flw_to_pkts.items():
            cutoff_idx = utils.find_bound(
                pkts[features.ARRIVAL_TIME_FET],
                end_s * 1e6,
                flw_to_last_cutoff_idx[flw],
                len(pkts) - 1,
                "before",
            )
            tpus_bps = utils.safe_tput_bps(
                pkts, flw_to_last_cutoff_idx[flw], cutoff_idx
            )
            maxmin_rate_bps = bneck_to_sender_to_maxminbps[bneck][flw_to_sender[flw]]
            flw_to_maxmin_ratio[flw] = tpus_bps / maxmin_rate_bps
            flw_to_last_cutoff_idx[flw] = cutoff_idx + 1
        bneck_to_maxmin_ratios[bneck] = list(flw_to_maxmin_ratio.values())
    return bneck_to_maxmin_ratios


def get_jfi(flw_to_pkts, servicepolicy=False, flw_to_sender=None):
    flw_to_tput_bps = {
        flw: 0 if len(pkts) == 0 else utils.safe_tput_bps(pkts, 0, len(pkts) - 1)
        for flw, pkts in flw_to_pkts.items()
    }
    if servicepolicy:
        assert flw_to_sender is not None
        sender_to_tput_bps = collections.defaultdict(float)
        for flw, tput_bps in flw_to_tput_bps.items():
            sender_to_tput_bps[flw_to_sender[flw]] += tput_bps
        values = sender_to_tput_bps.values()
    else:
        values = flw_to_tput_bps.values()

    return sum(values) ** 2 / (len(values) * sum(value**2 for value in values))


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
    matched_results,
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
                output_selector(vals)
                for exp, vals in matched_results.items()
                # Only select experiments for this category.
                if category_selector(exp, vals) == category
            ]
        )
        for category in {
            # First, determine the categories.
            category_selector(exp, vals)
            for exp, vals in matched_results.items()
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

    # logging.info(
    #     "Categories for %s:\n%s",
    #     filename,
    #     "\n\t".join(
    #         [
    #             (f"{category}:\n" + "\n\t\t".join(values))
    #             for category, values in category_to_values.items()
    #         ]
    #     ),
    # )

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


def get_params(exp_dir):
    """
    Load the params JSON file for the given experiment.

    exp_dir is an untarred individual experiment.
    """
    params_flp = path.join(exp_dir, f"{utils.Exp(exp_dir).name}.json")
    if not path.exists(params_flp):
        raise FileNotFoundError(
            f"Error: Cannot find params file ({params_flp}) in: {exp_dir}"
        )
    with open(params_flp, "r", encoding="utf-8") as fil:
        return json.load(fil)


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

    our_label = "ServicePolicy" if args.servicepolicy else "FlowPolicy"

    # Find all experiments.
    pcaps = [
        (
            path.join(args.exp_dir, exp),
            args.untar_dir,
            path.join(args.out_dir, "individual_results"),
            False,  # skip_smoothed
            args.select_tail_percent,
            args.servicepolicy,
            True,  # always_reparse
            parse_opened_exp,
        )
        for exp in sorted(os.listdir(args.exp_dir))
        if exp.endswith(".tar.gz")
    ]
    random.shuffle(pcaps)

    logging.info("Num files: %d", len(pcaps))
    start_time_s = time.time()

    data_flp = path.join(args.out_dir, "results.pickle")
    if path.exists(data_flp):
        logging.info("Loading data from: %s", data_flp)
        # Load existing raw JFI results.
        with open(data_flp, "rb") as fil:
            results = pickle.load(fil)
        if len(results) != len(pcaps):
            logging.warning(
                (
                    "Warning: Expected %d JFI results, but found %d. "
                    "Delete %s and try again."
                ),
                len(pcaps),
                len(results),
                data_flp,
            )
    else:
        if defaults.SYNC:
            results = [gen_features.parse_exp(*pcap) for pcap in pcaps]
        else:
            with multiprocessing.Pool(processes=args.parallel) as pol:
                results = pol.starmap(gen_features.parse_exp, pcaps)
        # Save raw JFI results from parsed experiments.
        with open(data_flp, "wb") as fil:
            pickle.dump(results, fil)

    # Dict mapping experiment to JFI.
    results = {
        exp_results[0]: tuple(exp_results[1:])
        for exp_results in results
        if (isinstance(exp_results, tuple) and -1 not in exp_results[1:])
    }

    # Determine the experiment category and make sure that all experiments are from the
    # same category.
    categories = set()
    for params, _, _, _, _ in results.values():
        categories.add(params["category"])
    assert len(categories) == 1, (
        "Error: Experiments must belong to the same category, "
        f"but these were found: {categories}"
    )
    category = categories.pop()
    # Extract the classifier and evaluation function for the given category.
    _, eval_func = CATEGORIES[category]

    # Experiments in which the ratemon was enabled.
    enabled = {exp for exp in results if exp.use_ratemon}
    # Experiments in which the ratemon was disabled.
    disabled = {exp for exp in results if not exp.use_ratemon}

    # Match each enabled experiment with its corresponding disabled experiment.
    # matched is a dict mapping the name of the experiment to a tuple of the form:
    #     ( params JSON, disabled results, enabled results )
    # where the two results entries are tuples of the form returned by
    # parse_opened_exp():
    #     ( jfi, overall util, map from class to util )
    matched = {}
    for enabled_exp in enabled:
        # Find the corresponding experiment with the ratemon disabled.
        target_disabled_name = enabled_exp.name.replace("unfairTrue", "unfairFalse")
        # Strip off trailing timestamp (everything after final "-").
        target_disabled_name = target_disabled_name[
            : -(target_disabled_name[::-1].index("-") + 1)
        ]
        target_disabled_exp = None
        for disabled_exp in disabled:
            if disabled_exp.name.startswith(target_disabled_name):
                target_disabled_exp = disabled_exp
                break
        if target_disabled_exp is None:
            logging.info(
                "Warning: Cannot find experiment with ratemon disabled: %s",
                target_disabled_name,
            )
            continue
        matched[enabled_exp] = (
            results[target_disabled_exp],
            results[enabled_exp],
        )
    logging.info("Matched experiments: %d", len(matched))

    # Call category-specific evaluation function.
    ret = eval_func(args, our_label, matched)

    logging.info("Done analyzing - time: %.2f seconds", time.time() - start_time_s)
    return ret


def eval_shared(args, our_label, matched):
    """Generate graphs for the simple shared bottleneck experiments."""
    matched_results = {}
    for enabled_exp, (disabled_results, enabled_results) in matched.items():
        (
            params,
            jfi_disabled,
            overall_util_disabled,
            class_to_util_disabled,
            _,
        ) = disabled_results
        (
            _,
            jfi_enabled,
            overall_util_enabled,
            class_to_util_enabled,
            _,
        ) = enabled_results

        assert tuple(sorted(class_to_util_disabled.keys())) == (0, 20)
        incumbent_flows_util_disabled = class_to_util_disabled[0]
        newcomer_flows_util_disabled = class_to_util_disabled[20]
        incumbent_flows_util_enabled = class_to_util_enabled[0]
        newcomer_flows_util_enabled = class_to_util_enabled[20]

        matched_results[enabled_exp] = (
            jfi_disabled,  # 0
            jfi_enabled,  # 1
            jfi_enabled - jfi_disabled,  # 2
            (jfi_enabled - jfi_disabled) / jfi_disabled * 100,  # 3
            overall_util_disabled * 100,  # 4
            overall_util_enabled * 100,  # 5
            (overall_util_enabled - overall_util_disabled) * 100,  # 6
            incumbent_flows_util_disabled * 100,  # 7
            incumbent_flows_util_enabled * 100,  # 8
            (incumbent_flows_util_enabled - incumbent_flows_util_disabled) * 100,  # 9
            newcomer_flows_util_disabled * 100,  # 10
            newcomer_flows_util_enabled * 100,  # 11
            (newcomer_flows_util_enabled - newcomer_flows_util_disabled) * 100,  # 12
            params,  # 13
        )
    # Save JFI results.
    with open(path.join(args.out_dir, "results.json"), "w", encoding="utf-8") as fil:
        json.dump(
            {exp.name: val for exp, val in matched_results.items()}, fil, indent=4
        )

    (
        jfis_disabled,
        jfis_enabled,
        _,  # jfi_deltas,
        jfi_deltas_percent,
        overall_utils_disabled,
        overall_utils_enabled,
        overall_util_deltas_percent,
        incumbent_flows_utils_disabled,
        incumbent_flows_utils_enabled,
        incumbent_flows_util_deltas_percent,
        newcomer_flows_utils_disabled,
        newcomer_flows_utils_enabled,
        newcomer_flows_util_deltas_percent,
        _,  # params
    ) = list(zip(*matched_results.values()))

    # Plot the fair rates in the experiment configurations so that we can see if the
    # randomly-chosen experiments are actually imbalanced.
    fair_rates_Mbps = [exp.target_per_flow_bw_Mbps for exp in matched_results]
    plot_cdf(
        args,
        lines=[fair_rates_Mbps],
        labels=["Fair rate"],
        x_label="Fair rate (Mbps)",
        x_max=max(fair_rates_Mbps),
        filename="fair_rate_cdf.pdf",
        linestyles=["solid"],
        colors=COLORS[:1],
        # title=f"CDF of fair rate",
    )
    plot_hist(
        args,
        lines=[fair_rates_Mbps],
        labels=["Fair rate"],
        x_label="Fair rate (Mbps)",
        filename="fair_rate_hist.pdf",
        # title="Histogram of fair rate",
    )

    plot_hist(
        args,
        lines=[jfis_disabled, jfis_enabled],
        labels=["Original", our_label],
        x_label="JFI",
        filename="jfi_hist.pdf",
        # title="Histogram of JFI,\nwith and without RateMon",
    )
    plot_hist(
        args,
        lines=[overall_utils_disabled, overall_utils_enabled],
        labels=["Original", our_label],
        x_label="Overall link utilization (%)",
        filename="overall_util_hist.pdf",
        # title="Histogram of overall link utilization,\nwith and without RateMon",
    )
    plot_hist(
        args,
        lines=[incumbent_flows_utils_disabled, incumbent_flows_utils_enabled],
        labels=["Original", our_label],
        x_label="Total link utilization of incumbent flows (%)",
        filename="incumbent_flows_util_hist.pdf",
        # title='Histogram of incumbent flows link utilization,\nwith and without RateMon',
    )
    plot_hist(
        args,
        lines=[newcomer_flows_utils_disabled, newcomer_flows_utils_enabled],
        labels=["Original", our_label],
        x_label="Link utilization of newcomer flow (%)",
        filename="newcomer_flows_util_hist.pdf",
        # title='Histogram of newcomer flow link utilization,\nwith and without RateMon',
    )
    plot_cdf(
        args,
        lines=[jfis_disabled, jfis_enabled],
        labels=["Original", our_label],
        x_label="JFI",
        x_max=1.0,
        filename="jfi_cdf.pdf",
        linestyles=["dashed", "dashdot"],
        colors=COLORS[:2],
        # title="CDF of JFI,\nwith and without RateMon",
    )
    plot_cdf(
        args,
        lines=[
            [100 - x for x in overall_utils_disabled],
            [100 - x for x in overall_utils_enabled],
        ],
        labels=["Original", our_label],
        x_label="Unused link capacity (%)",
        x_max=100,
        filename="unused_util_cdf.pdf",
        linestyles=["dashed", "dashdot"],
        colors=COLORS[:2],
        legendloc="lower right",
        # title="CDF of unused link capacity,\nwith and without RateMon",
    )
    plot_cdf(
        args,
        lines=[overall_utils_disabled, overall_utils_enabled],
        labels=["Original", our_label],
        x_label="Overall link utilization (%)",
        x_max=100,
        filename="util_cdf.pdf",
        linestyles=["dashed", "dashdot"],
        colors=COLORS[:2],
        legendloc="upper left",
        # title="CDF of overall link utilization,\nwith and without RateMon",
    )

    num_flows = [
        (
            # Incumbent flows (start at 0s).
            sum(
                flowset[9]
                for flowset in disabled_results[0]["flowsets"]
                if flowset[3] == 0
            ),
            # Newcomer flows (start at 20s).
            sum(
                flowset[9]
                for flowset in disabled_results[0]["flowsets"]
                if flowset[3] == 20
            ),
        )
        for _, (disabled_results, _) in matched.items()
    ]
    # Expected total utilization of incumbent and newcomer flows.
    incumbent_flows_fair_shares = [inc / (inc + new) * 100 for inc, new in num_flows]
    newcomer_flows_fair_shares = [new / (inc + new) * 100 for inc, new in num_flows]

    plot_cdf(
        args,
        lines=[
            # Expected total utilization of incumbent flows.
            incumbent_flows_fair_shares,
            incumbent_flows_utils_disabled,
            incumbent_flows_utils_enabled,
        ],
        labels=["Perfectly Fair", "Original", our_label],
        x_label="Total link utilization of incumbent flows (%)",
        x_max=100,
        filename="incumbent_flows_util_cdf.pdf",
        # title='CDF of incumbent flows link utilization,\nwith and without RateMon',
        colors=[COLORS_MAP["orange"], COLORS_MAP["red"], COLORS_MAP["blue"]],
    )
    plot_cdf(
        args,
        lines=[
            # Expected total utilization of newcomer flows.
            newcomer_flows_fair_shares,
            newcomer_flows_utils_disabled,
            newcomer_flows_utils_enabled,
        ],
        labels=["Perfectly Fair", "Original", our_label],
        x_label="Link utilization of newcomer flow (%)",
        x_max=100,
        filename="newcomer_flows_util_cdf.pdf",
        # title='CDF of newcomer flow link utilization,\nwith and without RateMon',
        colors=[COLORS_MAP["orange"], COLORS_MAP["red"], COLORS_MAP["blue"]],
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
            "\nIncumbent flows link utilization change "
            "--- higher is better, want to be >= 0%%:\n"
            "\tAvg: %s%.4f %%\n"
            "\tStddev: %.4f %%\n"
            "\tVar: %.4f %%"
        ),
        "+" if np.mean(incumbent_flows_util_deltas_percent) > 0 else "",
        np.mean(incumbent_flows_util_deltas_percent),
        np.std(incumbent_flows_util_deltas_percent),
        np.var(incumbent_flows_util_deltas_percent),
    )
    logging.info(
        (
            "\nNewcomer flow link utilization change "
            "--- higher is better, want to be >= 0%%:\n"
            "\tAvg: %s%.4f %%\n"
            "\tStddev: %.4f %%\n"
            "\tVar: %.4f %%"
        ),
        "+" if np.mean(newcomer_flows_util_deltas_percent) > 0 else "",
        np.mean(newcomer_flows_util_deltas_percent),
        np.std(newcomer_flows_util_deltas_percent),
        np.var(newcomer_flows_util_deltas_percent),
    )

    # Break down utilization based on experiment parameters.
    group_and_box_plot(
        args,
        matched_results,
        lambda exp, vals: exp.bw_Mbps,
        lambda result: result[5],
        lambda x: x,
        "Bandwidth (Mbps)",
        "Utilization (%)",
        100,
        "bandwidth_vs_util.pdf",
        num_buckets=10,
    )
    group_and_box_plot(
        args,
        matched_results,
        lambda exp, vals: exp.target_per_flow_bw_Mbps,
        lambda result: result[5],
        lambda x: x,
        "Fair rate (Mbps)",
        "Utilization (%)",
        100,
        "fair_rate_vs_util.pdf",
        num_buckets=10,
    )
    group_and_box_plot(
        args,
        matched_results,
        lambda exp, vals: exp.rtt_us,
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
        matched_results,
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
        matched_results,
        # Add up the total number of incumbent flows across the flowsets.
        lambda exp, vals: sum(
            flowset[9] for flowset in vals[-1]["flowsets"] if flowset[3] == 0
        ),
        lambda result: result[5],
        lambda x: x,
        "Incumbent flows",
        "utilization (%)",
        100,
        "incumbent_flows_vs_util.pdf",
        num_buckets=10,
    )

    # Break down JFI based on experiment parameters.
    group_and_box_plot(
        args,
        matched_results,
        lambda exp, vals: exp.bw_bps,
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
        matched_results,
        lambda exp, vals: exp.target_per_flow_bw_Mbps,
        lambda result: result[1],
        lambda x: x,
        "Fair rate (Mbps)",
        "JFI",
        1,
        "fair_rate_vs_jfi.pdf",
        num_buckets=10,
    )
    group_and_box_plot(
        args,
        matched_results,
        lambda exp, vals: exp.rtt_us,
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
        matched_results,
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
        matched_results,
        # Add up the total number of incumbent flows across the flowsets.
        lambda exp, vals: sum(
            flowset[9] for flowset in vals[-1]["flowsets"] if flowset[3] == 0
        ),
        lambda result: result[1],
        lambda x: x,
        "Incumbent flows",
        "JFI",
        1,
        "incumbent_flows_vs_jfi.pdf",
        num_buckets=10,
    )
    return 0


def eval_multibottleneck(args, our_label, matched):
    """Generate graphs for the multibottleneck flows experiments."""
    bneck_to_all_maxmin_ratios_disabled = collections.defaultdict(list)
    bneck_to_all_maxmin_ratios_enabled = collections.defaultdict(list)
    for _, (disabled_results, enabled_results) in matched.items():
        (
            _,
            _,
            _,
            _,
            bneck_to_maxmin_ratios_disabled,
        ) = disabled_results
        (
            _,
            _,
            _,
            _,
            bneck_to_maxmin_ratios_enabled,
        ) = enabled_results

        assert set(bneck_to_maxmin_ratios_disabled.keys()) == set(
            bneck_to_maxmin_ratios_enabled.keys()
        ), "Bottlenecks do not match between disabled and enabled experiments!"
        for bneck, maxmin_ratios in bneck_to_maxmin_ratios_disabled.items():
            bneck_to_all_maxmin_ratios_disabled[bneck].extend(maxmin_ratios)
        for bneck, maxmin_ratios in bneck_to_maxmin_ratios_enabled.items():
            bneck_to_all_maxmin_ratios_enabled[bneck].extend(maxmin_ratios)

    assert set(bneck_to_all_maxmin_ratios_disabled.keys()) == set(
        bneck_to_all_maxmin_ratios_enabled.keys()
    )
    bnecks = sorted(bneck_to_all_maxmin_ratios_disabled.keys(), key=lambda x: x[0])

    # For each bottleneck disuation, graph a CDF of the maxmin ratios.
    for bneck_idx, bneck in enumerate(bnecks):
        plot_cdf(
            args,
            lines=[
                bneck_to_all_maxmin_ratios_disabled[bneck],
                bneck_to_all_maxmin_ratios_enabled[bneck],
            ],
            labels=["Original", our_label],
            x_label="Ratio of throughput to maxmin fair rate (ideal = 1)",
            x_max=(
                1.01
                * max(
                    *bneck_to_all_maxmin_ratios_disabled[bneck],
                    *bneck_to_all_maxmin_ratios_enabled[bneck],
                )
            ),
            filename=f"bneck{bneck_idx}_maxmin_ratio.pdf",
            colors=[COLORS_MAP["red"], COLORS_MAP["blue"]],
        )

    bneck_to_change = {}
    for bneck_idx, bneck in enumerate(bnecks):
        start_s, end_s = bneck
        avg_disabled = np.average(bneck_to_all_maxmin_ratios_disabled[bneck])
        avg_enabled = np.average(bneck_to_all_maxmin_ratios_enabled[bneck])
        percent_change = (avg_enabled - avg_disabled) / avg_disabled * 100
        bneck_to_change[bneck_idx] = {
            "start_s": start_s,
            "end_s": end_s,
            "avg_disabled": avg_disabled,
            "avg_enabled": avg_enabled,
            "percent_change": percent_change,
        }

    with open(
        path.join(args.out_dir, "bneck_to_change.json"), "w", encoding="utf-8"
    ) as fil:
        json.dump(bneck_to_change, fil, indent=4)
    return 0


def eval_background(args, our_label, matched):
    """Generate graphs for the background flows experiments."""
    matched_results = {}
    for enabled_exp, (disabled_results, enabled_results) in matched.items():
        (
            params,
            jfi_disabled,
            overall_util_disabled,
            class_to_util_disabled,
            _,
        ) = disabled_results
        (
            _,
            jfi_enabled,
            overall_util_enabled,
            class_to_util_enabled,
            _,
        ) = enabled_results

        assert tuple(sorted(class_to_util_disabled.keys())) == ("receiver", "sink")
        foreground_flows_util_disabled = class_to_util_disabled["receiver"]
        background_flows_util_disabled = class_to_util_disabled["sink"]
        foreground_flows_util_enabled = class_to_util_enabled["receiver"]
        background_flows_util_enabled = class_to_util_enabled["sink"]

        matched_results[enabled_exp] = (
            jfi_disabled,  # 0
            jfi_enabled,  # 1
            jfi_enabled - jfi_disabled,  # 2
            (jfi_enabled - jfi_disabled) / jfi_disabled * 100,  # 3
            overall_util_disabled * 100,  # 4
            overall_util_enabled * 100,  # 5
            (overall_util_enabled - overall_util_disabled) * 100,  # 6
            foreground_flows_util_disabled * 100,  # 7
            foreground_flows_util_enabled * 100,  # 8
            (foreground_flows_util_enabled - foreground_flows_util_disabled) * 100,  # 9
            background_flows_util_disabled * 100,  # 10
            background_flows_util_enabled * 100,  # 11
            (background_flows_util_enabled - background_flows_util_disabled)
            * 100,  # 12
            params,  # 13
        )
    # Save JFI results.
    with open(path.join(args.out_dir, "results.json"), "w", encoding="utf-8") as fil:
        json.dump(
            {exp.name: val for exp, val in matched_results.items()}, fil, indent=4
        )

    (
        jfis_disabled,
        jfis_enabled,
        _,  # jfi_deltas,
        _,  # jfi_deltas_percent,
        overall_utils_disabled,
        overall_utils_enabled,
        _,  # overall_util_deltas_percent,
        foreground_flows_utils_disabled,
        foreground_flows_utils_enabled,
        _,  # foreground_flows_util_deltas_percent,
        background_flows_utils_disabled,
        background_flows_utils_enabled,
        _,  # background_flows_util_deltas_percent,
        _,  # params
    ) = list(zip(*matched_results.values()))

    num_flows = [
        (
            # Foreground flows (not to "sink").
            sum(
                flowset[9]
                for flowset in disabled_results[0]["flowsets"]
                if flowset[1][0] != "sink"
            ),
            # Background flows (to "sink").
            sum(
                flowset[9]
                for flowset in disabled_results[0]["flowsets"]
                if flowset[1][0] == "sink"
            ),
        )
        for _, (disabled_results, _) in matched.items()
    ]
    # Expected total utilization of foreground and background flows.
    foreground_flows_fair_shares = [
        fore / (fore + back) * 100 for fore, back in num_flows
    ]
    background_flows_fair_shares = [
        back / (fore + back) * 100 for fore, back in num_flows
    ]

    plot_cdf(
        args,
        lines=[
            foreground_flows_fair_shares,
            foreground_flows_utils_disabled,
            foreground_flows_utils_enabled,
        ],
        labels=["Perfectly Fair", "Original", our_label],
        x_label="Link utilization of all foreground flows (%)",
        x_max=100,
        filename="foreground_flows_util_cdf.pdf",
        # title='CDF of foreground flows link utilization,\nwith and without RateMon',
        colors=[COLORS_MAP["orange"], COLORS_MAP["red"], COLORS_MAP["blue"]],
    )
    plot_cdf(
        args,
        lines=[
            background_flows_fair_shares,
            background_flows_utils_disabled,
            background_flows_utils_enabled,
        ],
        labels=["Perfectly Fair", "Original", our_label],
        x_label="Link utilization of all background flows (%)",
        x_max=100,
        filename="background_flows_util_cdf.pdf",
        # title='CDF of background flow link utilization,\nwith and without RateMon',
        colors=[COLORS_MAP["orange"], COLORS_MAP["red"], COLORS_MAP["blue"]],
    )
    plot_cdf(
        args,
        lines=[jfis_disabled, jfis_enabled],
        labels=["Original", our_label],
        x_label="JFI",
        x_max=1.0,
        filename="jfi_cdf.pdf",
        linestyles=["dashed", "dashdot"],
        colors=COLORS[:2],
        # title="CDF of JFI,\nwith and without RateMon",
    )
    plot_cdf(
        args,
        lines=[overall_utils_disabled, overall_utils_enabled],
        labels=["Original", our_label],
        x_label="Overall link utilization (%)",
        x_max=100,
        filename="util_cdf.pdf",
        linestyles=["dashed", "dashdot"],
        colors=COLORS[:2],
        legendloc="upper left",
        # title="CDF of overall link utilization,\nwith and without RateMon",
    )
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
    parser.add_argument(
        "--prefix",
        help="A prefix to attach to output filenames.",
        required=False,
        type=str,
    )
    parser.add_argument(
        "--servicepolicy",
        action="store_true",
        help="Evaluate fairness across senders instead of across flows.",
    )
    args = parser.parse_args()
    assert path.isdir(args.exp_dir)
    assert path.isdir(args.out_dir)
    global PREFIX
    PREFIX = "" if args.prefix is None else f"{args.prefix}_"
    return args


# Used to group flows into classes based on a particular parameter.
# Examples of classes:
#     CCA, e.g., Cubic vs. BBR
#     Flow start time, e.g., incumbent vs. newcomer
#     Sender, e.g., sender-0 vs. sender-1.
#     Receiver, e.g., receiver-0 vs. sink.
# A flow's class is separate from whether ratemon is enabled or disabled at the
# experiment level
CLASSIFIERS = {
    "sender": lambda flw: flw[0][0],
    "receiver": lambda flw: flw[1][0],
    "cca": lambda flw: flw[2],
    "start_time_s": lambda flw: flw[3],
}
# The evaluation function is specific to the experiment category.
# "category" is configured in the original config JSON.
CATEGORIES = {
    "shared": ("start_time_s", eval_shared),
    "multibottleneck": ("sender", eval_multibottleneck),
    "background": ("receiver", eval_background),
}

if __name__ == "__main__":
    sys.exit(main(parse_args()))
