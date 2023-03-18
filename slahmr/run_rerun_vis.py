"""Visualize SLAHMR results with rerun."""

import os

import numpy as np
import rerun as rr
import torch
import trimesh
from omegaconf import OmegaConf
from scipy.spatial import transform

from slahmr.body_model import run_smpl
from slahmr.data import dataset, expand_source_paths, get_dataset_from_cfg
from slahmr.run_vis import get_input_dict, get_results_paths, load_result
from slahmr.util.loaders import (
    load_config_from_log,
    load_smpl_body_model,
    resolve_cfg_paths,
)
from slahmr.util.tensor import get_device, move_to, to_torch
import open3d as o3d


def log_to_rerun(
    cfg,
    dataset: dataset.MultiPeopleDataset,
    log_dir: str,
    dev_id,
    phases=["motion_chunks"],
    render_views=["src_cam", "above", "side"],
    make_grid=False,
    overwrite=False,
    render_kps=True,
    render_layers=False,
    save_frames=False,
    **kwargs,
) -> None:
    # first camera view defines world coordinate system
    # assuming camera is upright, -Y will be up
    rr.init("slahmr", spawn=True)
    rr.log_view_coordinates("world", up="-Y", timeless=True)

    log_input_frames(
        dataset,
    )

    log_skeleton_2d(dataset)

    # TODO this should be removed, part of input phase
    # there should be one camera per phase
    log_camera(dataset)

    for phase in phases:
        phase_dir = os.path.join(log_dir, phase)
        if phase == "input":
            res = get_input_dict(dataset)
            it = f"{0:06d}"

        elif os.path.isdir(phase_dir):
            res_path_dict = get_results_paths(phase_dir)
            it = sorted(res_path_dict.keys())[-1]
            res = load_result(res_path_dict[it])["world"]
        else:
            print(f"{phase_dir} does not exist, skipping")
            continue

        log_phase_result(cfg, dataset, dev_id, phase, res)


def log_camera(dataset: dataset.MultiPeopleDataset) -> None:
    """Log camera trajectory to rerun."""
    cam_data = dataset.get_camera_data()
    num_frames = dataset.seq_len
    for frame_id in range(num_frames):
        translation = cam_data["cam_t"][frame_id]
        rotation_mat = cam_data["cam_R"][frame_id]
        rotation_q = transform.Rotation.from_matrix(rotation_mat).as_quat()
        fx, fy, cx, cy = cam_data["intrins"][frame_id]
        width, height = dataset.img_size
        rr.set_time_sequence("input_frame_id", frame_id)
        rr.log_pinhole(
            "world/camera/image",
            child_from_parent=[[fx, 0, cx], [0, fy, cy], [0, 0, 1]],
            width=width,
            height=height,
        )
        rr.log_rigid3(
            "world/camera",
            child_from_parent=(translation.numpy(), rotation_q),
            xyz="RDF",
        )


def log_phase_result(
    cfg, dataset: dataset.MultiPeopleDataset, dev_id, phase: str, phase_result: dict
) -> None:
    """Log results from one phase."""
    B = len(dataset)
    num_frames = dataset.seq_len
    vis_mask = dataset.data_dict["vis_mask"]  # -1 out of frame, 0 occluded, 1 visible
    device = get_device(dev_id)
    phase_result = move_to(phase_result, device)

    cfg = resolve_cfg_paths(cfg)
    body_model, _ = load_smpl_body_model(cfg.paths.smpl, B * num_frames, device=device)

    with torch.no_grad():
        world_smpl = run_smpl(
            body_model,
            phase_result["trans"],
            phase_result["root_orient"],
            phase_result["pose_body"],
            phase_result.get("betas", None),
        )

    vertices = world_smpl["vertices"].numpy(force=True)
    faces = world_smpl["faces"].numpy(force=True)

    for frame_id in range(num_frames):
        rr.set_time_sequence("input_frame_id", frame_id)
        translation = phase_result["cam_t"][1, frame_id].numpy(force=True)
        rotation_mat = phase_result["cam_R"][1, frame_id].numpy(force=True)
        rotation_q = transform.Rotation.from_matrix(rotation_mat).as_quat()
        rr.log_rigid3(
            "world/camera",
            child_from_parent=(translation, rotation_q),
            xyz="RDF",
        )
        for i, _ in enumerate(dataset.track_ids):
            if vis_mask[i][frame_id] >= 0:
                mesh = trimesh.Trimesh(vertices[i, frame_id], faces)
                vertex_normals = mesh.vertex_normals
                rr.log_mesh(
                    f"world/phase_{phase}/#{i}",
                    vertices[i][frame_id],
                    indices=faces,
                    normals=vertex_normals,
                )
            else:
                rr.log_cleared(
                    f"world/phase_{phase}/#{i}",
                )


def log_input_frames(dataset: dataset.MultiPeopleDataset) -> None:
    """Log raw input video to rerun."""
    for frame_id, img_path in enumerate(dataset.sel_img_paths):
        rr.set_time_sequence("input_frame_id", frame_id)
        rr.log_image_file("world/camera/image", img_path=img_path)


def log_skeleton_2d(dataset: dataset.MultiPeopleDataset) -> None:
    """Log 2D skeleton to rerun."""
    dataset.load_data()
    for i, track_id in enumerate(dataset.track_ids):
        joints2d = dataset.data_dict["joints2d"][i]  # (T, J, 3)
        for frame_id, frame_joints in enumerate(joints2d):
            # show the results
            skeleton_ids = np.array(
                [
                    [15, 13],
                    [13, 11],
                    [16, 14],
                    [14, 12],
                    [11, 12],
                    [5, 11],
                    [6, 12],
                    [5, 6],
                    [5, 7],
                    [6, 8],
                    [7, 9],
                    [8, 10],
                    [1, 2],
                    [0, 1],
                    [0, 2],
                    [1, 3],
                    [2, 4],
                    [3, 5],
                    [4, 6],
                ]
            )

            idcs = [0, 16, 15, 18, 17, 5, 2, 6, 3, 7, 4, 12, 9, 13, 10, 14, 11]
            joints = frame_joints[idcs][skeleton_ids]
            joint_confidence = joints[..., 2].min(axis=-1)  # min conf per joint
            good_joints_xy = joints[joint_confidence > 0.3, :, :2]

            rr.set_time_sequence("input_frame_id", frame_id)
            if len(good_joints_xy):
                rr.log_line_segments(
                    f"world/camera/image/skeleton/#{i}", good_joints_xy.reshape(-2, 2)
                )
            else:
                # NOTE how to best handle skeleton out of view?
                # lower alpha might be nicer
                rr.log_cleared(f"world/camera/image/skeleton/#{i}")


def log_to_rrd(log_dir: str, dev_id, phases, save_dir=None, **kwargs):
    print(log_dir)
    cfg = load_config_from_log(log_dir)

    # make sure we get all necessary inputs
    cfg.data.sources = expand_source_paths(cfg.data.sources)
    print("SOURCES", cfg.data.sources)
    dataset = get_dataset_from_cfg(cfg)
    if len(dataset) < 1:
        print("No tracks in dataset, skipping")
        return

    log_to_rerun(cfg, dataset, log_dir, dev_id, phases=phases, **kwargs)
    rr.save(os.path.join(save_dir, "log.rrd"))


def launch_rerun_vis(i, args):
    log_dir = args.log_dirs[i]
    dev_id = args.gpus[i % len(args.gpus)]
    os.environ["EGL_DEVICE_ID"] = str(dev_id)
    os.environ["PYOPENGL_PLATFORM"] = "egl"
    path_name = log_dir.split(args.log_root)[-1].strip("/")
    exp_name = "-".join(path_name.split("/")[:2])

    if args.save_root is not None:
        save_dir = f"{args.save_root}/{exp_name}"
        os.makedirs(save_dir, exist_ok=True)
    else:
        save_dir = log_dir
        os.makedirs(save_dir, exist_ok=True)

    log_to_rrd(
        log_dir,
        dev_id,
        phases=args.phases,
        save_dir=save_dir,
        overwrite=args.overwrite,
        accumulate=args.accumulate,
        render_kps=args.render_kps,
        render_layers=args.render_layers,
        render_views=args.render_views,
        save_frames=args.save_frames,
        make_grid=args.grid,
    )


def main(args):
    """
    visualize all runs in root
    """
    OmegaConf.register_new_resolver("eval", eval)
    log_dirs = []
    for root, subd, files in os.walk(args.log_root):
        if ".hydra" in subd:
            log_dirs.append(root)
    args.log_dirs = log_dirs
    print(f"FOUND {len(args.log_dirs)} TO RENDER")

    if len(args.gpus) > 1:
        from torch.multiprocessing import Pool

        torch.multiprocessing.set_start_method("spawn")

        with Pool(processes=len(args.gpus)) as pool:
            res = pool.starmap(
                launch_rerun_vis, [(i, args) for i in range(len(args.log_dirs))]
            )
        return

    for i in range(len(args.log_dirs)):
        launch_rerun_vis(i, args)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--log_root", required=True)
    parser.add_argument("--save_root", default=None)
    parser.add_argument(
        "--phases",
        nargs="*",
        # default=["input", "init", "motion_chunks", "root_fit", "smooth_fit"],
        default=["motion_chunks"],
    )
    parser.add_argument("--gpus", nargs="*", default=[0])
    parser.add_argument(
        "-rv",
        "--render_views",
        nargs="*",
        default=["src_cam", "front", "above", "side"],
    )
    parser.add_argument("-g", "--grid", action="store_true")
    parser.add_argument("-rl", "--render_layers", action="store_true")
    parser.add_argument("-kp", "--render_kps", action="store_true")
    parser.add_argument("-sf", "--save_frames", action="store_true")
    parser.add_argument("-ra", "--accumulate", action="store_true")
    parser.add_argument("-y", "--overwrite", action="store_true")
    args = parser.parse_args()

    main(args)
