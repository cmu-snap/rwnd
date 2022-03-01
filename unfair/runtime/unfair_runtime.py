#!/usr/bin/python3
"""Monitors incoming TCP flows to detect unfairness."""

from argparse import ArgumentParser
import ctypes
from os import path
import pickle
import socket
import struct
import sys
import threading
import time

import torch
from pyroute2 import IPRoute
from pyroute2 import protocols
from pyroute2.netlink.exceptions import NetlinkError

from bcc import BPF

from unfair.model import data, defaults, features, gen_features, models, utils
from unfair.runtime import mitigation_strategy, reaction_strategy
from unfair.runtime.mitigation_strategy import MitigationStrategy
from unfair.runtime.reaction_strategy import ReactionStrategy


def ip_str_to_int(ip_str):
    """Convert an IP address string in dotted-quad notation to an integer."""
    return struct.unpack("<L", socket.inet_aton(ip_str))[0]


LOCALHOST = ip_str_to_int("127.0.0.1")
# Flows that have not received a new packet in this many seconds will be
# garbage collected.
OLD_THRESH_SEC = 5 * 60


class FlowKey(ctypes.Structure):
    """A struct to use as the key in maps in the corresponding eBPF program."""

    _fields_ = [
        ("saddr", ctypes.c_uint),
        ("daddr", ctypes.c_uint),
        ("sport", ctypes.c_ushort),
        ("dport", ctypes.c_ushort),
    ]


class Flow:
    """Represents a single flow, identified by a four-tuple.

    A Flow object is created when we first receive a packet for the flow. A Flow object
    is deleted when it has been five minutes since we have checked this flow's
    fairness.

    Must acquire self.lock before accessing members.
    """

    def __init__(self, fourtuple):
        """Set up data structures for a flow."""
        self.lock = threading.RLock()
        self.fourtuple = fourtuple
        self.packets = []
        # Smallest RTT ever observed for this flow (microseconds). Used to calculate
        # the BDP. Updated whenever we compute features for this flow.
        self.min_rtt_us = sys.maxsize
        # The timestamp of the last packet on which we have run inference.
        self.latest_time_sec = 0
        self.label = defaults.Class.APPROX_FAIR
        self.decision = (defaults.Decision.NOT_PACED, None)

    def __str__(self):
        """Create a string representation of this flow."""
        return flow_to_str(self.fourtuple)


def int_to_ip_str(ip_int):
    """Convert an IP address int into a dotted-quad string."""
    # Use "<" (little endian) instead of "!" (network / big endian) because the
    # IP addresses are already stored in little endian.
    return socket.inet_ntoa(struct.pack("<L", ip_int))


def flow_to_str(fourtuple):
    """Convert a flow four-tuple into a string."""
    saddr, daddr, sport, dport = fourtuple
    return f"{int_to_ip_str(saddr)}:{sport} -> {int_to_ip_str(daddr)}:{dport}"


def flow_data_to_str(dat):
    """Convert a flow data tuple into a string."""
    (
        seq,
        srtt_us,
        tsval,
        tsecr,
        total_bytes,
        ihl_bytes,
        thl_bytes,
        payload_bytes,
        time_us,
    ) = dat
    return (
        f"seq: {seq}, srtt: {srtt_us} us, tsval: {tsval}, tsecr: {tsecr}, "
        f"total: {total_bytes} B, IP header: {ihl_bytes} B, "
        f"TCP header: {thl_bytes} B, payload: {payload_bytes} B, "
        f"time: {time.ctime(time_us / 1e3)}"
    )


def receive_packet(flows, flows_lock, pkt, done):
    """Ingest a new packet, identify its flow, and store it.

    This function delegates its main tasks to receive_packet_helper() and instead just
    guards that function by bypassing it if the done event is set and by catching
    KeyboardInterrupt exceptions.

    flows_lock protects flows.
    """
    try:
        if not done.is_set():
            receive_packet_helper(flows, flows_lock, pkt)
    except KeyboardInterrupt:
        done.set()


def receive_packet_helper(flows, flows_lock, pkt):
    """Ingest a new packet, identify its flow, and store it.

    flows_lock protects flows.
    """
    # Skip packets on the loopback interface.
    if LOCALHOST in (pkt.saddr, pkt.daddr):
        return

    # flow = (pkt.flow.saddr, pkt.flow.daddr, pkt.flow.sport, pkt.flow.dport)
    fourtuple = (pkt.saddr, pkt.daddr, pkt.sport, pkt.dport)
    dat = (
        pkt.seq,
        pkt.srtt_us,
        pkt.tsval,
        pkt.tsecr,
        pkt.total_bytes,
        pkt.ihl_bytes,
        pkt.thl_bytes,
        pkt.payload_bytes,
        pkt.time_us,
    )

    # Attempt to acquire flows_lock. If unsuccessful, skip this packet.
    if flows_lock.acquire(blocking=False):
        try:
            if fourtuple in flows:
                flow = flows[fourtuple]
            if fourtuple not in flows:
                flow = Flow(fourtuple)
                flows[fourtuple] = flow

            # Attempt to acquire the lock for this flow. If unsuccessful, then skip this
            # packet.
            if flow.lock.acquire(blocking=False):
                try:
                    flow.packets.append(dat)
                finally:
                    flow.lock.release()
                print(f"{flow} --- {flow_data_to_str(dat)}")
        finally:
            flows_lock.release()


def check_loop(flows, flows_lock, net, args, flow_to_rwnd, done):
    """Periodically evaluate flow fairness.

    Intended to be run as the target function of a thread.
    """
    try:
        while not done.is_set():
            check_flows(
                flows,
                flows_lock,
                args.limit,
                net,
                flow_to_rwnd,
                args,
            )

            with flows_lock:
                # Do not bother acquiring the per-flow locks since we are just reading
                # data for logging purposes. It is okay if we get inconsistent
                # information.
                print(
                    "Current decisions:\n"
                    + "\n".join(f"\t{flow}: {flow.decision}" for flow in flows.values())
                )

            time.sleep(args.interval_ms / 1e3)
    except KeyboardInterrupt:
        done.set()


def check_flows(flows, flows_lock, limit, net, flow_to_rwnd, args):
    """Identify flows that are ready to be checked, and check them.

    Remove old and empty flows.
    """
    print("Examining flows...")
    to_remove = []
    to_check = []

    # Need to acquire flows_lock while iterating over flows.
    with flows_lock:
        print(f"Found {len(flows)} flows total:")
        for fourtuple, flow in flows.items():
            print(f"\t{flow} - {len(flow.packets)} packets")
            # Try to acquire the lock for this flow. If unsuccessful, do not block; move
            # on to the next flow.
            if flow.lock.acquire(blocking=False):
                try:
                    if len(flow.packets) >= limit:
                        # Plan to run inference on "full" flows.
                        to_check.append(fourtuple)
                    elif flow.latest_time_sec and (
                        time.time() - flow.latest_time_sec > OLD_THRESH_SEC
                    ):
                        # Remove flows with no packets and flows that have not received
                        # a new packet in five seconds.
                        to_remove.append(fourtuple)
                finally:
                    flow.lock.release()
            else:
                print(f"Cloud not acquire lock for flow {flow}")
        # Garbage collection.
        print(f"Removing {len(to_remove)} flows:")
        for fourtuple in to_remove:
            print(f"\t{flows[fourtuple]}")
            del flows[fourtuple]
            del flow_to_rwnd[FlowKey(*fourtuple)]

    print(f"Checking {len(to_check)} flows...")
    for fourtuple in to_check:
        flow = flows[fourtuple]
        # Try to acquire the lock for this flow. If unsuccessful, do not block. Move on
        # to the next flow.
        if flow.lock.acquire(blocking=False):
            try:
                print(f"Checking flow {flow}")
                if not args.disable_inference:
                    check_flow(flows, fourtuple, net, flow_to_rwnd, args)
            finally:
                flow.lock.release()
        else:
            print(f"Cloud not acquire lock for flow {flow}")


def featurize(flows, fourtuple, net, pkts, debug=False):
    """Compute features for the provided list of packets.

    Returns a structured numpy array.
    """
    flow = flows[fourtuple]
    with flow.lock:
        fets, flow.min_rtt_us = gen_features.parse_received_acks(
            net.in_spc, fourtuple, pkts, flow.min_rtt_us, debug
        )

    data.replace_unknowns(fets, isinstance(net, models.HistGbdtSklearnWrapper))
    return fets


def packets_to_ndarray(pkts):
    """Reorganize a list of packet metrics into a structured numpy array."""
    (
        seqs,
        srtts_us,
        tsvals,
        tsecrs,
        totals_bytes,
        _,
        _,
        payloads_bytes,
        times_us,
    ) = zip(*pkts)
    pkts = utils.make_empty(len(seqs), additional_dtype=[(features.SRTT_FET, "int32")])
    pkts[features.SEQ_FET] = seqs
    pkts[features.ARRIVAL_TIME_FET] = times_us
    pkts[features.TS_1_FET] = tsvals
    pkts[features.TS_2_FET] = tsecrs
    pkts[features.PAYLOAD_FET] = payloads_bytes
    pkts[features.WIRELEN_FET] = totals_bytes
    pkts[features.SRTT_FET] = srtts_us
    return pkts


def make_decision(flows, fourtuple, pkts_ndarray, flow_to_rwnd, reaction_strategy):
    """Make a flow unfairness mitigation decision.

    Base the decision on the flow's label and existing decision. Use the flow's packets
    to calculate any necessary flow metrics, such as the throughput.

    TODO: Instead of passing in using the flow's packets, pass in the features and make
          sure that they include the necessary columns.
    """
    flow = flows[fourtuple]
    with flow.lock:
        tput_bps = utils.safe_tput_bps(pkts_ndarray, 0, len(pkts_ndarray) - 1)

        if flow.label == defaults.Class.ABOVE_FAIR:
            # This flow is sending too fast. Force the sender to halve its rate.
            new_decision = (
                defaults.Decision.PACED,
                reaction_strategy.react_down(
                    reaction_strategy, utils.bdp_B(tput_bps, flow.min_rtt_us / 1e6)
                ),
            )
        elif flow.decision == defaults.Decision.PACED:
            # We are already pacing this flow.
            if flow.label == defaults.Class.BELOW_FAIR:
                # If we are already pacing this flow but we are being too aggressive,
                # then let it send faster.
                new_decision = (
                    defaults.Decision.PACED,
                    reaction_strategy.react_up(
                        reaction_strategy, utils.bdp_B(tput_bps, flow.min_rtt_us / 1e6)
                    ),
                )
            else:
                # If we are already pacing this flow and it is behaving as desired, then
                # all is well. Retain the existing pacing decision.
                new_decision = flow.decision
        else:
            # This flow is not already being paced and is not behaving unfairly, so
            # leave it alone.
            new_decision = (defaults.Decision.NOT_PACED, None)

        if flow.decision != new_decision:
            flow_to_rwnd[FlowKey(*flow.fourtuple)] = ctypes.c_int(
                round(new_decision[1])
            )

        flow.decision = new_decision


def condense_labels(labels):
    """Combine multiple labels into a single label.

    For example, smooth the labels by selecting the average label.

    Currently, this simply selects the last label.
    """
    assert len(labels) > 0, "Labels cannot be empty."
    return labels[-1]


def check_flow(flows, fourtuple, net, flow_to_rwnd, args):
    """Determine whether a flow is unfair and how to mitigate it.

    Runs inference on a flow's packets and determines the appropriate ACK
    pacing for the flow. Updates the flow's fairness record.
    """
    flow = flows[fourtuple]
    with flow.lock:
        # Discard all but the most recent 100 packets.
        if len(flow.packets) > 100:
            flow.packets = flow.packets[-100:]
        pkts_ndarray = packets_to_ndarray(flow.packets)

        # Record the time at which we check this flow.
        flow.latest_time_sec = time.time()

        start_time_s = time.time()
        labels = inference(flows, fourtuple, net, pkts_ndarray, args.debug)
        print(f"Inference took: {(time.time() - start_time_s) * 1e3} ms")

        flow.label = condense_labels(labels)
        make_decision(
            flows, fourtuple, pkts_ndarray, flow_to_rwnd, args.reaction_strategy
        )
        print(f"Report for flow {flow}: {flow.label}, {flow.decision}")

        # Clear the flow's packets.
        flow.packets = []


def inference(flows, fourtuple, net, pkts, debug=False):
    """Run inference on a flow's packets.

    Returns a label: below fair, approximately fair, above fair.
    """
    preds = net.predict(
        torch.tensor(
            utils.clean(featurize(flows, fourtuple, net, pkts, debug)),
            dtype=torch.float,
        )
    )
    return [defaults.Class(pred) for pred in preds]


def _main():
    parser = ArgumentParser(description="Squelch unfair flows.")
    parser.add_argument("-i", "--interval-ms", help="Poll interval (ms)", type=float)
    parser.add_argument(
        "-d", "--debug", action="store_true", help="Print debugging info"
    )
    parser.add_argument(
        "--interface",
        help='The network interface to attach to (e.g., "eno1").',
        required=True,
        type=str,
    )
    parser.add_argument(
        "-l",
        "--limit",
        default=100,
        help=("The number of packets to accumulate for a flow between inference runs."),
        type=int,
    )
    parser.add_argument(
        "-s",
        "--disable-inference",
        action="store_true",
        help="Disable periodic inference.",
    )
    parser.add_argument(
        "--model",
        choices=models.MODEL_NAMES,
        help="The model to use.",
        required=True,
        type=str,
    )
    parser.add_argument(
        "-f", "--model-file", help="The trained model to use.", required=True, type=str
    )
    parser.add_argument(
        "--reaction-strategy",
        choices=reaction_strategy.choices(),
        default=reaction_strategy.to_str(ReactionStrategy.MIMD),
        help="The reaction/feedback strategy to use.",
        type=str,
    )
    parser.add_argument(
        "--mitigation-strategy",
        choices=mitigation_strategy.choices(),
        default=mitigation_strategy.to_str(MitigationStrategy.RWND_TUNING),
        help="The unfairness mitigation strategy to use.",
        type=str,
    )
    args = parser.parse_args()

    assert args.limit > 0, f'"--limit" must be greater than 0 but is: {args.limit}'
    assert path.isfile(args.model_file), f"Model does not exist: {args.model_file}"

    net = models.MODELS[args.model]()
    with open(args.model_file, "rb") as fil:
        net.net = pickle.load(fil)

    # Maps each flow (four-tuple) to a list of packets for that flow. New
    # packets are appended to the ends of these lists. Periodically, a flow's
    # packets are consumed by the inference engine and that flow's list is
    # reset to empty.
    flows = dict()  # defaultdict(list)
    # Maps each flow (four-tuple) to a tuple of fairness state:
    #   (is_fair, response)
    # where is_fair is either -1 (no label), 0 (below fair), 1 (approximately
    # fair), or 2 (above fair) and response is a either ACK pacing rate or RWND
    #  value.
    # fairness_db = dict()  # defaultdict(lambda: (-1, 0))

    # Lock for the packet input data structures (e.g., "flows"). Only acquire this lock
    # when adding, removing, or iterating over flows; no need to acquire this lock when
    # updating a flow object.
    flows_lock = threading.RLock()

    done = threading.Event()
    done.clear()

    # Load BPF text.
    bpf_flp = path.join(
        path.abspath(path.dirname(__file__)),
        path.basename(__file__).strip().split(".")[0] + ".c",
    )
    if not path.isfile(bpf_flp):
        print(f"Could not find BPF program: {bpf_flp}")
        return 1
    print(f"Loading BPF program: {bpf_flp}")
    with open(bpf_flp, "r", encoding="utf-8") as fil:
        bpf_text = fil.read()
    if args.debug:
        print(bpf_text)

    # Load BPF program.
    bpf = BPF(text=bpf_text)
    bpf.attach_kprobe(event="tcp_rcv_established", fn_name="trace_tcp_rcv")
    # bpf.attach_kprobe(event="tc_egress", fn_name="trace_tc_egress")
    egress_fn = bpf.load_func("handle_egress", BPF.SCHED_ACT)

    flow_to_rwnd = bpf["flow_to_rwnd"]

    # Set up the inference thread.
    check_thread = threading.Thread(
        target=check_loop,
        args=(flows, flows_lock, net, args, flow_to_rwnd, done),
    )
    check_thread.start()

    # # Configure unfairness mitigation strategy.

    ipr = IPRoute()
    ifindex = ipr.link_lookup(ifname=args.interface)
    assert len(ifindex) == 1
    ifindex = ifindex[0]
    # # ipr.tc("add", "pfifo", 0, "1:")
    # # ipr.tc("add-filter", "bpf", 0, ":1", fd=egress_fn.fd, name=egress_fn.name, parent="1:")

    # There can also be a chain of actions, which depend on the return
    # value of the previous action.
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
        print("Error: Unable to configure TC.")
        return 1

    # This function will be called to process an event from the BPF program.
    def process_event(cpu, dat, size):
        receive_packet(flows, flows_lock, bpf["pkts"].event(dat), done)

    bpf["pkts"].open_perf_buffer(process_event)

    print("Running...press Control-C to end")
    try:
        # Loop with callback to process_event().
        while not done.is_set():
            if args.interval_ms is not None:
                time.sleep(args.interval_ms / 1000)
            bpf.perf_buffer_poll()
    except KeyboardInterrupt:
        print("Cancelled.")
        # global DONE
        # DONE = True
        done.set()
    finally:
        print("Cleaning up...")
        ipr.tc("del", "htb", ifindex, 0x10000, default=0x200000)

    # print("\nFlows:")
    # for flow, pkts in sorted(flows.items()):
    #     print("\t", flow_to_str(flow), len(pkts))

    check_thread.join()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
