import logging
import os
import time
from contextlib import nullcontext
from glob import glob

import eqnet
import matplotlib
import pandas as pd
import torch
import torch.multiprocessing as mp
import torch.utils.data
import utils
import wandb
from eqnet.data import DASIterableDataset, SeismicTraceIterableDataset
from eqnet.models.unet import moving_normalize
from eqnet.utils import (
    detect_peaks,
    extract_picks,
    merge_csvs,
    merge_patch,
    plot_das,
    plot_phasenet,
)
from tqdm.auto import tqdm

# mp.set_start_method("spawn", force=True)
matplotlib.use("agg")
logger = logging.getLogger()


def postprocess(meta, output):
    nt, nx = meta["nt"], meta["nx"]
    data = meta["data"][:, :, :nt, :nx]
    # data = moving_normalize(data)
    meta["data"] = data
    if "phase" in output:
        output["phase"] = output["phase"][:, :, :nt, :nx]
    if "polarity" in output:
        output["polarity"] = output["polarity"][:, :, :nt, :nx]
    if "event" in output:
        output["event"] = output["event"][:, :, :nt, :nx]
    return meta, output


def pred_phasenet(args, model, data_loader, pick_path, figure_path, event_path=None):
    model.eval()
    metric_logger = utils.MetricLogger(delimiter="  ")
    header = "Predicting:"
    # ctx = nullcontext() if args.device == "cpu" else torch.cuda.amp.autocast(enabled=args.amp)
    ctx = nullcontext() if args.device == "cpu" else torch.amp.autocast(device_type=args.device, dtype=args.ptdtype)
    with torch.inference_mode():
        # for meta in metric_logger.log_every(data_loader, 1, header):
        for meta in tqdm(data_loader, desc="Predicting", total=len(data_loader)):
            with ctx:
                output = model(meta)
                meta, output = postprocess(meta, output)
            if "phase" in output:
                phase_scores = torch.softmax(output["phase"], dim=1)  # [batch, nch, nt, nsta]
                if ("polarity" in output) and (output["polarity"] is not None):
                    polarity_scores = (torch.sigmoid(output["polarity"]) - 0.5) * 2.0
                else:
                    polarity_scores = None
                topk_phase_scores, topk_phase_inds = detect_peaks(phase_scores, vmin=args.min_prob, kernel=128)
                phase_picks_ = extract_picks(
                    topk_phase_inds,
                    topk_phase_scores,
                    file_name=meta["file_name"],
                    station_id=meta["station_id"],
                    begin_time=meta["begin_time"] if "begin_time" in meta else None,
                    begin_time_index=meta["begin_time_index"] if "begin_time_index" in meta else None,
                    dt=meta["dt_s"] if "dt_s" in meta else 0.01,
                    vmin=args.min_prob,
                    phases=args.phases,
                    polarity_score=polarity_scores,
                    waveform=meta["data"],
                    window_amp=[10, 5],  # s
                )

            if ("event" in output) and (output["event"] is not None):
                event_scores = torch.sigmoid(output["event"])
                topk_event_scores, topk_event_inds = detect_peaks(event_scores, vmin=args.min_prob, kernel=128)
                event_picks_ = extract_picks(
                    topk_event_inds,
                    topk_event_scores,
                    file_name=meta["file_name"],
                    station_id=meta["station_id"],
                    begin_time=meta["begin_time"] if "begin_time" in meta else None,
                    begin_time_index=meta["begin_time_index"] if "begin_time_index" in meta else None,
                    ## event are picked on downsampled time resolution
                    dt=meta["dt_s"] * 16 if "dt_s" in meta else 0.01 * 16,
                    vmin=args.min_prob,
                    phases=["event"],
                )

            for i in range(len(meta["file_name"])):
                # filename = meta["file_name"][i].split("//")[-1].replace("/", "_")
                # filename = meta["file_name"][i].split("/")[-1].replace("*", "")
                ## filename convention year/jday/station_id
                tmp = meta["file_name"][i].split("/")
                parent_dir = "/".join(tmp[-args.folder_depth : -1])
                filename = tmp[-1].replace("*", "").replace("?", "").replace(".mseed", "")

                if not os.path.exists(os.path.join(pick_path, parent_dir)):
                    os.makedirs(os.path.join(pick_path, parent_dir), exist_ok=True)
                if len(phase_picks_[i]) == 0:
                    ## keep an empty file for the file with no picks to make it easier to track processed files
                    with open(os.path.join(pick_path, parent_dir, filename + ".csv"), "a"):
                        pass
                    continue
                picks_df = pd.DataFrame(phase_picks_[i])
                # picks_df["phase_time"] = picks_df["phase_time"].apply(lambda x: x.isoformat(timespec="milliseconds"))
                picks_df.sort_values(by=["phase_time"], inplace=True)
                picks_df.to_csv(os.path.join(pick_path, parent_dir, filename + ".csv"), index=False)

                if "event" in output:
                    if not os.path.exists(os.path.join(event_path, parent_dir)):
                        os.makedirs(os.path.join(event_path, parent_dir), exist_ok=True)
                    if len(event_picks_[i]) == 0:
                        with open(os.path.join(event_path, parent_dir, filename + ".csv"), "a"):
                            pass
                        continue
                    picks_df = pd.DataFrame(event_picks_[i])
                    # picks_df["phase_time"] = picks_df["phase_time"].apply(
                    #     lambda x: x.isoformat(timespec="milliseconds")
                    # )
                    picks_df.sort_values(by=["phase_time"], inplace=True)
                    picks_df.to_csv(os.path.join(event_path, parent_dir, filename + ".csv"), index=False)

            if args.plot_figure:
                # meta["waveform_raw"] = meta["waveform"].clone()
                # meta["data"] = moving_normalize(meta["data"])
                plot_phasenet(
                    meta,
                    phase_scores.cpu(),
                    event_scores.cpu() if "event" in output else None,
                    polarity=polarity_scores.cpu() if polarity_scores is not None else None,
                    picks=phase_picks_,
                    phases=args.phases,
                    file_name=meta["file_name"],
                    dt=meta["dt_s"] if "dt_s" in meta else torch.tensor(0.01),
                    figure_dir=figure_path,
                )
                print("saving:", meta["file_name"])

    ## merge picks
    if args.distributed:
        torch.distributed.barrier()
        if utils.is_main_process():
            merge_csvs(pick_path)
            merge_csvs(event_path)
    else:
        merge_csvs(pick_path)
        merge_csvs(event_path)
    return 0


def pred_phasenet_das(args, model, data_loader, pick_path, figure_path):
    model.eval()
    metric_logger = utils.MetricLogger(delimiter="  ")
    header = "Predicting:"
    # ctx = nullcontext() if args.device == "cpu" else torch.cuda.amp.autocast(enabled=args.amp)
    ctx = nullcontext() if args.device == "cpu" else torch.amp.autocast(device_type=args.device, dtype=args.ptdtype)
    with torch.inference_mode():
        # for meta in metric_logger.log_every(data_loader, 1, header):
        for meta in tqdm(data_loader, desc="Predicting", total=len(data_loader)):
            with ctx:
                output = model(meta)

            meta, output = postprocess(meta, output)
            scores = torch.softmax(output["phase"], dim=1)  # [batch, nch, nt, nsta]
            topk_scores, topk_inds = detect_peaks(scores, vmin=args.min_prob, kernel=21)

            picks_ = extract_picks(
                topk_inds,
                topk_scores,
                file_name=meta["file_name"],
                begin_time=meta["begin_time"] if "begin_time" in meta else None,
                begin_time_index=meta["begin_time_index"] if "begin_time_index" in meta else None,
                begin_channel_index=meta["begin_channel_index"] if "begin_channel_index" in meta else None,
                dt=meta["dt_s"] if "dt_s" in meta else 0.01,
                vmin=args.min_prob,
                phases=args.phases,
            )

            for i in range(len(meta["file_name"])):
                tmp = meta["file_name"][i].split("/")
                parent_dir = "/".join(tmp[-args.folder_depth : -1])
                filename = tmp[-1].replace("*", "").replace(f".{args.format}", "")
                if not os.path.exists(os.path.join(pick_path, parent_dir)):
                    os.makedirs(os.path.join(pick_path, parent_dir), exist_ok=True)

                if len(picks_[i]) == 0:
                    ## keep an empty file for the file with no picks to make it easier to track processed files
                    with open(os.path.join(pick_path, parent_dir, filename + ".csv"), "a"):
                        pass
                    continue
                picks_df = pd.DataFrame(picks_[i])
                picks_df["channel_index"] = picks_df["station_id"].apply(lambda x: int(x))
                picks_df.sort_values(by=["channel_index", "phase_index"], inplace=True)
                picks_df.to_csv(
                    os.path.join(pick_path, parent_dir, filename + ".csv"),
                    columns=["channel_index", "phase_index", "phase_time", "phase_score", "phase_type"],
                    index=False,
                )

            if args.plot_figure:
                plot_das(
                    meta["data"].cpu().float(),
                    scores.cpu().float(),
                    picks=picks_,
                    phases=args.phases,
                    file_name=meta["file_name"],
                    begin_time_index=meta["begin_time_index"] if "begin_time_index" in meta else None,
                    begin_channel_index=meta["begin_channel_index"] if "begin_channel_index" in meta else None,
                    dt=meta["dt_s"] if "dt_s" in meta else torch.tensor(0.01),
                    dx=meta["dx_m"] if "dx_m" in meta else torch.tensor(10.0),
                    figure_dir=figure_path,
                )

    if args.distributed:
        torch.distributed.barrier()
        if args.cut_patch and utils.is_main_process():
            merge_patch(pick_path, pick_path.rstrip("_patch"), return_single_file=False)
    else:
        if args.cut_patch:
            merge_patch(pick_path, pick_path.rstrip("_patch"), return_single_file=False)

    return 0


def main(args):
    result_path = args.result_path
    if args.cut_patch:
        pick_path = os.path.join(result_path, f"picks_{args.model}_patch")
        event_path = os.path.join(result_path, f"events_{args.model}_patch")
        figure_path = os.path.join(result_path, f"figures_{args.model}_patch")
    else:
        pick_path = os.path.join(result_path, f"picks_{args.model}")
        event_path = os.path.join(result_path, f"events_{args.model}")
        figure_path = os.path.join(result_path, f"figures_{args.model}")
    if not os.path.exists(result_path):
        utils.mkdir(result_path)
    if not os.path.exists(pick_path):
        utils.mkdir(pick_path)
    if not os.path.exists(event_path):
        utils.mkdir(event_path)
    if not os.path.exists(figure_path):
        utils.mkdir(figure_path)

    utils.init_distributed_mode(args)
    print(args)

    if args.distributed:
        rank = utils.get_rank()
        world_size = utils.get_world_size()
    else:
        rank, world_size = 0, 1
    device = torch.device(args.device)
    dtype = "bfloat16" if torch.cuda.is_available() and torch.cuda.is_bf16_supported() else "float16"
    ptdtype = {"float32": torch.float32, "bfloat16": torch.bfloat16, "float16": torch.float16}[dtype]
    args.dtype, args.ptdtype = dtype, ptdtype
    torch.backends.cuda.matmul.allow_tf32 = True  # allow tf32 on matmul
    torch.backends.cudnn.allow_tf32 = True  # allow tf32 on cudnn
    if args.use_deterministic_algorithms:
        torch.backends.cudnn.benchmark = False
        torch.use_deterministic_algorithms(True)
    else:
        torch.backends.cudnn.benchmark = True

    if args.model == "phasenet":
        dataset = SeismicTraceIterableDataset(
            data_path=args.data_path,
            data_list=args.data_list,
            hdf5_file=args.hdf5_file,
            prefix=args.prefix,
            format=args.format,
            dataset=args.dataset,
            training=False,
            highpass_filter=args.highpass_filter,
            response_xml=args.response_xml,
            cut_patch=args.cut_patch,
            resample_time=args.resample_time,
            system=args.system,
            nx=args.nx,
            nt=args.nt,
            rank=rank,
            world_size=world_size,
        )
        sampler = None
    elif args.model == "phasenet_das":
        dataset = DASIterableDataset(
            data_path=args.data_path,
            data_list=args.data_list,
            format=args.format,
            nx=args.nx,
            nt=args.nt,
            training=False,
            system=args.system,
            cut_patch=args.cut_patch,
            highpass_filter=args.highpass_filter,
            resample_time=args.resample_time,
            resample_space=args.resample_space,
            skip_existing=args.skip_existing,
            pick_path=pick_path,
            rank=rank,
            world_size=world_size,
        )
        sampler = None
    else:
        raise ("Unknown model")

    data_loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=args.batch_size,
        sampler=sampler,
        num_workers=min(args.workers, mp.cpu_count()),
        collate_fn=None,
        drop_last=False,
    )

    model = eqnet.models.__dict__[args.model].build_model(
        backbone=args.backbone,
        in_channels=1,
        out_channels=(len(args.phases) + 1),
        add_polarity=args.add_polarity,
        add_event=args.add_event,
    )
    logger.info("Model:\n{}".format(model))

    model.to(device)
    if args.distributed:
        model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)

    if args.resume:
        checkpoint = torch.load(args.resume, map_location="cpu")
        model.load_state_dict(checkpoint["model"], strict=True)
        print("Loaded checkpoint '{}' (epoch {})".format(args.resume, checkpoint["epoch"]))
    else:
        if args.model == "phasenet" and (not args.add_polarity):
            raise ("No pretrained model for phasenet, please use phasenet_polarity instead")
        elif (args.model == "phasenet") and (args.add_polarity):
            model_url = "https://github.com/AI4EPS/models/releases/download/PhaseNet-Polarity-v3/model_99.pth"
        elif args.model == "phasenet_das":
            if args.location is None:
                # model_url = "ai4eps/model-registry/PhaseNet-DAS:latest"
                # model_url = "https://github.com/AI4EPS/models/releases/download/PhaseNet-DAS-v0/PhaseNet-DAS-v0.pth"
                model_url = "https://github.com/AI4EPS/models/releases/download/PhaseNet-DAS-v1/PhaseNet-DAS-v1.pth"
            elif args.location == "forge":
                model_url = (
                    "https://github.com/AI4EPS/models/releases/download/PhaseNet-DAS-ConvertedPhase/model_99.pth"
                )
            else:
                raise ("Missing pretrained model for this location")
        else:
            raise

        ## load model from wandb
        # if utils.is_main_process():
        #     with wandb.init() as run:
        #         artifact = run.use_artifact(model_url, type="model")
        #         artifact_dir = artifact.download()
        #     checkpoint = torch.load(glob(os.path.join(artifact_dir, "*.pth"))[0], map_location="cpu")
        #     model.load_state_dict(checkpoint["model"], strict=True)

    model_without_ddp = model
    if args.distributed:
        torch.distributed.barrier()
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])
        model_without_ddp = model.module
    ## load model from url
    state_dict = torch.hub.load_state_dict_from_url(
        model_url, model_dir="./", progress=True, check_hash=True, map_location="cpu"
    )
    model_without_ddp.load_state_dict(state_dict["model"], strict=True)

    if args.model == "phasenet":
        pred_phasenet(args, model, data_loader, pick_path, figure_path, event_path)

    if args.model == "phasenet_das":
        pred_phasenet_das(args, model, data_loader, pick_path, figure_path)


def get_args_parser(add_help=True):
    import argparse

    parser = argparse.ArgumentParser(description="EQNet Model", add_help=add_help)

    # model
    parser.add_argument("--model", default="phasenet_das", type=str, help="model name")
    parser.add_argument("--resume", default="", type=str, help="path of checkpoint")
    parser.add_argument("--backbone", default="unet", type=str, help="model backbone")
    parser.add_argument("--phases", default=["P", "S"], type=str, nargs="+", help="phases to use")

    parser.add_argument("--device", default="cuda", type=str, help="device (Use cuda or cpu Default: cuda)")
    parser.add_argument(
        "-j", "--workers", default=4, type=int, metavar="N", help="number of data loading workers (default: 16)"
    )
    parser.add_argument(
        "-b", "--batch_size", default=1, type=int, help="images per gpu, the total batch size is $NGPU x batch_size"
    )
    # Mixed precision training parameters
    parser.add_argument(
        "--use_deterministic_algorithms", action="store_true", help="Forces the use of deterministic algorithms only."
    )
    parser.add_argument("--amp", action="store_true", help="Use torch.cuda.amp for mixed precision training")

    # distributed training parameters
    parser.add_argument("--world-size", default=1, type=int, help="number of distributed processes")
    parser.add_argument("--dist-url", default="env://", type=str, help="url used to set up distributed training")

    # prediction parameters
    parser.add_argument("--data_path", type=str, default="./", help="path to data directory")
    parser.add_argument("--data_list", type=str, default=None, help="selectecd data list")
    parser.add_argument("--hdf5-file", default=None, type=str, help="hdf5 file for training")
    parser.add_argument("--prefix", default="", type=str, help="prefix for the file name")
    parser.add_argument("--format", type=str, default="h5", help="data format")
    parser.add_argument("--dataset", type=str, default="das", help="dataset type; seismic_trace, seismic_network, das")
    parser.add_argument("--result_path", type=str, default="results", help="path to result directory")
    parser.add_argument("--plot_figure", action="store_true", help="If plot figure for test")
    parser.add_argument("--min_prob", default=0.3, type=float, help="minimum probability for picking")

    ## Seismic
    parser.add_argument("--add_polarity", action="store_true", help="If use polarity information")
    parser.add_argument("--add_event", action="store_true", help="If use event information")
    parser.add_argument("--highpass_filter", type=float, default=0.0, help="highpass filter; default 0.0 is no filter")
    parser.add_argument("--response_xml", default=None, type=str, help="response xml file")
    parser.add_argument("--folder_depth", default=0, type=int, help="folder depth for data list")

    ## DAS
    parser.add_argument("--cut_patch", action="store_true", help="If cut patch for continuous data")
    parser.add_argument("--nt", default=1024 * 20, type=int, help="number of time samples for each patch")
    parser.add_argument("--nx", default=1024 * 5, type=int, help="number of spatial samples for each patch")
    parser.add_argument("--resample_time", action="store_true", help="If resample time for continuous data")
    parser.add_argument("--resample_space", action="store_true", help="If resample space for continuous data")
    parser.add_argument(
        "--system", type=str, default=None, help="The name of system of different system: optasense, eqnet, or None"
    )
    parser.add_argument("--location", type=str, default=None, help="The name of systems at location")
    parser.add_argument("--skip_existing", action="store_true", help="Skip existing files")

    return parser


if __name__ == "__main__":
    args = get_args_parser().parse_args()
    main(args)
