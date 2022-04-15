"""This module defines a process that will receive packets and run inference on them."""

import collections
import ctypes
import logging
import os
from os import path
import queue
import signal
import sys
import time
import traceback

from bcc import BPF, BPFAttachType
from pyroute2 import IPRoute, protocols
from pyroute2.netlink.exceptions import NetlinkError
import torch

from unfair.model import data, defaults, features, gen_features, models, utils
from unfair.runtime import flow_utils, reaction_strategy
from unfair.runtime.reaction_strategy import ReactionStrategy


def featurize(flowkey, net, pkts, min_rtt_us, debug=False):
    """Compute features for the provided list of packets.

    Returns a structured numpy array.
    """
    fets, min_rtt_us = gen_features.parse_received_acks(
        list(net.in_spc), flowkey, pkts, min_rtt_us, debug
    )

    # Drop all but the ten most recent packets.
    fets = fets[-10:]

    data.replace_unknowns(fets, isinstance(net, models.HistGbdtSklearnWrapper))
    return fets, min_rtt_us


def inference(net, flowkey, pkts, min_rtt_us, debug=False):
    """Run inference on a flow's packets.

    Returns a label: below fair, approximately fair, above fair.
    """
    fets, min_rtt_us = featurize(flowkey, net, pkts, min_rtt_us, debug)

    msg = f"Features after featurize: {list(net.in_spc)} \n"
    for i in fets[-10:]:
        msg += f"{i}\n"
    logging.info(msg)

    cleaned = utils.clean(fets)

    msg = f"Features after clean: {list(net.in_spc)} \n"
    for i in cleaned[-10:]:
        msg += f"{i}\n"
    logging.info(msg)

    preds = net.predict(
        torch.tensor(
            cleaned,
            dtype=torch.float,
        )
    )
    return [defaults.Class(pred) for pred in preds], min_rtt_us


def condense_labels(labels):
    """Combine multiple labels into a single label.

    For example, smooth the labels by selecting the average label.

    Currently, this simply selects the last label.
    """
    assert len(labels) > 0, "Labels cannot be empty."
    return labels[-1]


def make_decision(
    flowkey, label, pkts_ndarray, min_rtt_us, decisions, flow_to_rwnd, args
):
    """Make a flow unfairness mitigation decision.

    Base the decision on the flow's label and existing decision. Use the flow's packets
    to calculate any necessary flow metrics, such as the throughput.

    TODO: Instead of passing in using the flow's packets, pass in the features and make
          sure that they include the necessary columns.
    """
    if args.reaction_strategy == ReactionStrategy.FILE:
        new_decision = (
            defaults.Decision.PACED,
            reaction_strategy.get_scheduled_pacing(args.schedule),
        )
    else:
        tput_bps = utils.safe_tput_bps(pkts_ndarray, 0, len(pkts_ndarray) - 1)

        if label == defaults.Class.ABOVE_FAIR:
            # This flow is sending too fast. Force the sender to halve its rate.
            new_decision = (
                defaults.Decision.PACED,
                reaction_strategy.react_down(
                    args.reaction_strategy,
                    utils.bdp_B(tput_bps, min_rtt_us / 1e6),
                ),
            )
        elif decisions[flowkey] == defaults.Decision.PACED:
            # We are already pacing this flow.
            if label == defaults.Class.BELOW_FAIR:
                # If we are already pacing this flow but we are being too
                # aggressive, then let it send faster.
                new_decision = (
                    defaults.Decision.PACED,
                    reaction_strategy.react_up(
                        args.reaction_strategy,
                        utils.bdp_B(tput_bps, min_rtt_us / 1e6),
                    ),
                )
            else:
                # If we are already pacing this flow and it is behaving as desired,
                # then all is well. Retain the existing pacing decision.
                new_decision = decisions[flowkey]
        else:
            # This flow is not already being paced and is not behaving unfairly, so
            # leave it alone.
            new_decision = (defaults.Decision.NOT_PACED, None)

    # FIXME: Why are the BDP calculations coming out so small? Is the throughput
    #        just low due to low application demand?

    logging.info("Decision for flow %s: %s", flowkey, new_decision)
    if decisions[flowkey] != new_decision:
        if new_decision[1] is None:
            del flow_to_rwnd[flowkey]
        else:
            new_decision = (new_decision[0], round(new_decision[1]))
            assert new_decision[1] > 0, (
                "Error: RWND must be greater than 0, "
                f"but is {new_decision[1]} for flow {flowkey}."
            )
            # if new_decision[1] > 2**16:
            #     logging.info(f"Warning: Asking for RWND >= 2**16: {new_decision[1]}")
            #     new_decision[1] = 2**16 - 1

            flow_to_rwnd[flowkey] = ctypes.c_uint32(new_decision[1])

        decisions[flowkey] = new_decision


def packets_to_ndarray(pkts):
    """Reorganize a list of packet metrics into a structured numpy array."""
    # For some reason, the packets tend to get reordered after they are timestamped on
    # arrival. Sort packets by timestamp.
    pkts = sorted(pkts, key=lambda pkt: pkt[-1])
    (
        seqs,
        rtts_us,
        # tsvals,
        # tsecrs,
        totals_bytes,
        # _,
        # _,
        payloads_bytes,
        times_us,
    ) = zip(*pkts)
    pkts = utils.make_empty(len(seqs), additional_dtype=[(features.RTT_FET, "int64")])
    pkts[features.SEQ_FET] = seqs
    pkts[features.ARRIVAL_TIME_FET] = times_us
    # pkts[features.TS_1_FET] = tsvals
    # pkts[features.TS_2_FET] = tsecrs
    pkts[features.PAYLOAD_FET] = payloads_bytes
    pkts[features.WIRELEN_FET] = totals_bytes
    pkts[features.RTT_FET] = rtts_us
    return pkts


def load_bpf(debug=False):
    """Load the corresponding eBPF program."""
    # Load BPF text.
    bpf_flp = path.join(
        path.abspath(path.dirname(__file__)),
        "unfair_runtime.c",
    )
    if not path.isfile(bpf_flp):
        logging.error("Could not find BPF program: %s", bpf_flp)
        return 1
    logging.info("Loading BPF program: %s", bpf_flp)
    with open(bpf_flp, "r", encoding="utf-8") as fil:
        bpf_text = fil.read()
    if debug:
        logging.debug(bpf_text)

    # Load BPF program.
    return BPF(text=bpf_text)


def configure_ebpf(args):
    """Set up eBPF hooks."""
    bpf = load_bpf(args.debug)

    # Read the TCP window scale on outgoing SYN-ACK packets.
    func_sock_ops = bpf.load_func("read_win_scale", bpf.SOCK_OPS)  # sock_stuff
    filedesc = os.open(args.cgroup, os.O_RDONLY)
    bpf.attach_func(func_sock_ops, filedesc, BPFAttachType.CGROUP_SOCK_OPS)

    # Overwrite advertised window size in outgoing packets.
    egress_fn = bpf.load_func("handle_egress", BPF.SCHED_ACT)
    flow_to_rwnd = bpf["flow_to_rwnd"]
    # Set up a TC egress qdisc, specify a filter the accepts all packets, and attach
    # our egress function as the action on that filter.
    ipr = IPRoute()
    ifindex = ipr.link_lookup(ifname=args.interface)
    assert (
        len(ifindex) == 1
    ), f'Trouble looking up index for interface "{args.interface}": {ifindex}'
    ifindex = ifindex[0]
    # ipr.tc("add", "pfifo", 0, "1:")
    # ipr.tc("add-filter", "bpf", 0, ":1", fd=egress_fn.fd, name=egress_fn.name, parent="1:")
    action = dict(kind="bpf", fd=egress_fn.fd, name=egress_fn.name, action="ok")
    try:
        # Add the action to a u32 match-all filter
        ipr.tc("add", "htb", ifindex, 0x10000, default=0x200000)
        ipr.tc(
            "add-filter",
            "u32",
            ifindex,
            parent=0x10000,
            prio=10,
            protocol=protocols.ETH_P_ALL,  # Every packet
            target=0x10020,
            keys=["0x0/0x0+0"],
            action=action,
        )
    except NetlinkError:
        logging.error("Error: Unable to configure TC.")
        return None, None

    def ebpf_cleanup():
        """Clean attached eBPF programs."""
        logging.info("Detaching sock_ops hook...")
        bpf.detach_func(func_sock_ops, filedesc, BPFAttachType.CGROUP_SOCK_OPS)

        logging.info("Removing egress TC...")
        ipr.tc("del", "htb", ifindex, 0x10000, default=0x200000)

    return flow_to_rwnd, ebpf_cleanup


def inference_loop(args, flow_to_rwnd, que, inference_flags, done):
    """Receive packets and run inference on them."""
    net = models.load_model(args.model_file)

    # TODO: This is a hack to shorten the number of packets we need to accumulate to
    #       run the model.
    net.in_spc = tuple(fet.replace("1024", "16") for fet in net.in_spc)

    min_rtts_us = collections.defaultdict(lambda: sys.maxsize)
    decisions = collections.defaultdict(lambda: (defaults.Decision.NOT_PACED, None))

    logging.info("Inference ready!")
    while not done.is_set():
        try:
            val = que.get(timeout=1)
            # For some reason, the queue returns True or None when the thread on the
            # other end dies.
            if not isinstance(val, tuple):
                continue
            fourtuple, pkts = val
        except queue.Empty:
            continue

        start_time_s = time.time()
        flowkey = flow_utils.FlowKey(*fourtuple)
        pkts_ndarray = packets_to_ndarray(pkts)
        try:
            labels, min_rtts_us[flowkey] = inference(
                net, flowkey, pkts_ndarray, min_rtts_us[flowkey], args.debug
            )
        except AssertionError:
            # Assertion errors mean this batch of packets violated some precondition,
            # but we are safe to skip them and continue.
            logging.warning(
                "Inference failed due to an assertion failure:\n%s",
                traceback.format_exc(),
            )
            continue
        except Exception as exp:
            # An unexpected error occurred. It is not safe to continue. Reraise the
            # exception to kill the process.
            logging.error(
                "Inference failed due to an unexpected error:\n%s",
                traceback.format_exc(),
            )
            raise exp
        else:
            # Inference succeeded.
            make_decision(
                flowkey,
                condense_labels(labels),
                pkts_ndarray,
                min_rtts_us[flowkey],
                decisions,
                flow_to_rwnd,
                args,
            )
        finally:
            logging.info("Inference took: %.2f ms", (time.time() - start_time_s) * 1e3)
            inference_flags[fourtuple].value = 0
            # TODO: Garbage collect flow_to_rwnd.


def run(args, que, inference_flags, done):
    """Receive packets and run inference on them.

    This function is designed to be the target of a process.
    """
    logging.basicConfig(
        filename=args.inference_log,
        format="%(asctime)s %(levelname)s %(message)s",
        level=logging.DEBUG,
    )
    cleanup = None

    def signal_handler(sig, frame):
        logging.info("Inference process: You pressed Ctrl+C!")
        done.set()

    signal.signal(signal.SIGINT, signal_handler)

    try:
        flow_to_rwnd, cleanup = configure_ebpf(args)
        if flow_to_rwnd is None:
            return
        inference_loop(args, flow_to_rwnd, que, inference_flags, done)
    except KeyboardInterrupt:
        logging.info("Cancelled.")
        done.set()
    finally:
        if cleanup is not None:
            cleanup()
