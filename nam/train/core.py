# File: core.py
# Created Date: Tuesday December 20th 2022
# Author: Steven Atkinson (steven@atkinson.mn)

"""
Functions used by the GUI trainer.
"""

import hashlib
from enum import Enum
from time import time
from typing import Optional, Sequence, Union

import matplotlib.pyplot as plt
import numpy as np
import pytorch_lightning as pl
import torch
import torch.nn as nn
from torch.utils.data import DataLoader

from ..data import REQUIRED_RATE, Split, init_dataset, wav_to_np, wav_to_tensor
from ..models import Model
from ..models.losses import esr
from ._version import Version


class Architecture(Enum):
    STANDARD = "standard"
    LITE = "lite"
    FEATHER = "feather"


def _detect_input_version(input_path) -> Version:
    """
    Check to see if the input matches any of the known inputs
    """

    def detect_strong(input_path) -> Optional[Version]:
        def assign_hash(path):
            # Use this to create hashes for new files
            md5 = hashlib.md5()
            buffer_size = 65536
            with open(input_path, "rb") as f:
                while True:
                    data = f.read(buffer_size)
                    if not data:
                        break
                    md5.update(data)
            file_hash = md5.hexdigest()
            return file_hash

        file_hash = assign_hash(input_path)
        print(f"Strong hash: {file_hash}")

        version = {
            "4d54a958861bf720ec4637f43d44a7ef": Version(1, 0, 0),
            "7c3b6119c74465f79d96c761a0e27370": Version(1, 1, 1),
            "cff9de79975f7fa2ba9060ad77cde04d": Version(2, 0, 0),
        }.get(file_hash)
        if version is None:
            print(
                f"Provided input file {input_path} does not strong-match any known "
                "standard input files."
            )
        return version

    def detect_weak(input_path) -> Optional[Version]:
        def assign_hash(path):
            # Use this to create recognized hashes for new files
            x, info = wav_to_np(path, info=True)
            rate = info.rate
            if rate != REQUIRED_RATE:
                return None
            # Times of intervals, in seconds
            t_blips = 1
            t_sweep = 3
            t_white = 3
            t_validation = 9
            # v1 and v2 start with 1 blips, sine sweeps, and white noise
            start_hash = hashlib.md5(
                x[: (t_blips + t_sweep + t_white) * rate]
            ).hexdigest()
            # v1 ends with validation signal
            end_hash_v1 = hashlib.md5(x[-t_validation * rate :]).hexdigest()
            # v2 ends with 2x validation & blips
            end_hash_v2 = hashlib.md5(
                x[-(2 * t_validation + t_blips) * rate :]
            ).hexdigest()
            return start_hash, end_hash_v1, end_hash_v2

        start_hash, end_hash_v1, end_hash_v2 = assign_hash(input_path)
        print(
            "Weak hashes:\n"
            f" Start:    {start_hash}\n"
            f" End (v1): {end_hash_v1}\n"
            f" End (v2): {end_hash_v2}\n",
        )

        # Check for v2 matches first
        version = {
            (
                "068a17d92274a136807523baad4913ff",
                "74f924e8b245c8f7dce007765911545a",
            ): Version(2, 0, 0)
        }.get((start_hash, end_hash_v2))
        if version is not None:
            return version
        # Fallback to v1
        version = {
            (
                "bb4e140c9299bae67560d280917eb52b",
                "9b2468fcb6e9460a399fc5f64389d353",
            ): Version(1, 0, 0),
            (
                "9f20c6b5f7fef68dd88307625a573a14",
                "8458126969a3f9d8e19a53554eb1fd52",
            ): Version(1, 1, 1),
        }.get((start_hash, end_hash_v1))
        return version

    version = detect_strong(input_path)
    if version is not None:
        return version
    print("Falling back to weak-matching...")
    version = detect_weak(input_path)
    if version is None:
        raise ValueError(
            f"Input file at {input_path} cannot be recognized as any known version!"
        )
    return version


_V1_BLIP_LOCATIONS = 12_000, 36_000
_V2_START_BLIP_LOCATIONS = _V1_BLIP_LOCATIONS
_V2_END_BLIP_LOCATIONS = -36_000, -12_000


def _calibrate_delay_v1(
    input_path, output_path, locations: Sequence[int] = _V1_BLIP_LOCATIONS
) -> int:
    lookahead = 1_000
    lookback = 10_000
    safety_factor = 4

    # Calibrate the trigger:
    y = wav_to_np(output_path)[:48_000]
    background_level = np.max(np.abs(y[:6_000]))
    trigger_threshold = max(background_level + 0.01, 1.01 * background_level)

    delays = []
    for blip_index, i in enumerate(locations, 1):
        start_looking = i - lookahead
        stop_looking = i + lookback
        y_scan = y[start_looking:stop_looking]
        triggered = np.where(np.abs(y_scan) > trigger_threshold)[0]
        if len(triggered) == 0:
            msg = (
                f"No response activated the trigger in response to blip "
                f"{blip_index}. Is something wrong with the reamp?"
            )
            print(msg)
            print("SHARE THIS PLOT IF YOU ASK FOR HELP")
            plt.figure()
            plt.plot(np.arange(-lookahead, lookback), y_scan, label="Signal")
            plt.axvline(x=0, color="C1", linestyle="--", label="Trigger")
            plt.axhline(
                y=-trigger_threshold, color="k", linestyle="--", label="Threshold"
            )
            plt.axhline(y=trigger_threshold, color="k", linestyle="--")
            plt.xlim((-lookahead, lookback))
            plt.xlabel("Samples")
            plt.ylabel("Response")
            plt.legend()
            plt.show()
            raise RuntimeError(msg)
        else:
            j = triggered[0]
            delays.append(j + start_looking - i)

    print("Delays:")
    for d in delays:
        print(f" {d}")
    delay = int(np.min(delays)) - safety_factor
    print(f"After aplying safety factor, final delay is {delay}")
    return delay


_calibrate_delay_v2 = _calibrate_delay_v1


def _plot_delay_v1(delay: int, input_path: str, output_path: str, _nofail=True):
    print("Plotting the delay for manual inspection...")
    x = wav_to_np(input_path)[:48_000]
    y = wav_to_np(output_path)[:48_000]
    i = np.where(np.abs(x) > 0.5 * np.abs(x).max())[0]  # In case resampled poorly
    if len(i) == 0:
        print("Failed to find the spike in the input file.")
        print(
            "Plotting the input and output; there should be spikes at around the "
            "marked locations."
        )
        expected_spikes = 12_000, 36_000  # For v1 specifically
        fig, axs = plt.subplots(2, 1)
        for ax, curve in zip(axs, (x, y)):
            ax.plot(curve)
            [ax.axvline(x=es, color="C1", linestyle="--") for es in expected_spikes]
        plt.show()
        if _nofail:
            raise RuntimeError("Failed to plot delay")
    else:
        i = i[0]
        di = 20
        plt.figure()
        # plt.plot(x[i - di : i + di], ".-", label="Input")
        plt.plot(
            np.arange(-di, di),
            y[i - di + delay : i + di + delay],
            ".-",
            label="Output",
        )
        plt.axvline(x=0, linestyle="--", color="C1")
        plt.legend()
        plt.show()  # This doesn't freeze the notebook


_plot_delay_v2 = _plot_delay_v1


def _calibrate_delay(
    delay: Optional[int],
    input_version: Version,
    input_path: str,
    output_path: str,
    silent: bool = False,
) -> int:
    if input_version.major == 1:
        calibrate, plot = _calibrate_delay_v1, _plot_delay_v1
    elif input_version.major == 2:
        calibrate, plot = _calibrate_delay_v2, _plot_delay_v2
    else:
        raise NotImplementedError(
            f"Input calibration not implemented for input version {input_version}"
        )
    if delay is not None:
        print(f"Delay is specified as {delay}")
    else:
        print("Delay wasn't provided; attempting to calibrate automatically...")
        delay = calibrate(input_path, output_path)
    if not silent:
        plot(delay, input_path, output_path)
    return delay


def _check_v1(*args, **kwargs):
    return True


def _check_v2(input_path, output_path) -> bool:
    print("V2 checks...")
    rate = REQUIRED_RATE
    y = wav_to_tensor(output_path, rate=rate)
    y_val_1 = y[-19 * rate : -10 * rate]
    y_val_2 = y[-10 * rate : -1 * rate]
    esr_replicate = esr(y_val_1, y_val_2).item()
    print(f"Replicate ESR is {esr_replicate:.8f}.")
    # Do the blips line up?
    # [start/end,replicate]
    blips = [
        [y[: rate // 2], y[rate // 2 : rate]],
        [y[-rate : -rate // 2], y[-rate // 2 :]],
    ]
    mse = nn.MSELoss()
    mse_0 = mse(blips[0][0], blips[0][1]).item()  # Within start
    mse_1 = mse(blips[1][0], blips[1][1]).item()  # Within end
    mse_cross_0 = mse(blips[0][0], blips[1][0]).item()  # 1st repeat, start vs end
    mse_cross_1 = mse(blips[0][1], blips[1][1]).item()  # 2nd repeat, start vs end

    mse_max = max(mse_0, mse_1)
    # mse_range = mse_max - min(mse_0, mse_1)
    safety_factor = 2.0
    if mse_cross_0 > safety_factor * mse_max or mse_cross_1 > safety_factor * mse_max:
        plt.plot()
        [
            [
                plt.plot(b, label=f"{startend}, replicate {replicate}")
                for replicate, b in enumerate(bi, 1)
            ]
            for startend, bi in zip(("start", "end"), blips)
        ]
        plt.xlabel("Sample")
        plt.ylabel("Output")
        plt.legend()
        plt.grid()
        print(
            "Failed blip checks. Did something change between the start and end of reamping?"
        )
        plt.show()
        return False
    return True


def _check(input_path: str, output_path: str, input_version: Version) -> bool:
    """
    Ensure that everything should go smoothly

    :return: True if looks good
    """
    if input_version.major == 1:
        f = _check_v1
    elif input_version.major == 2:
        f = _check_v2
    else:
        print(f"Checks not implemented for input version {input_version}; skip")
        return True
    return f(input_path, output_path)


def _get_wavenet_config(architecture):
    return {
        Architecture.STANDARD: {
            "layers_configs": [
                {
                    "input_size": 1,
                    "condition_size": 1,
                    "channels": 16,
                    "head_size": 8,
                    "kernel_size": 3,
                    "dilations": [1, 2, 4, 8, 16, 32, 64, 128, 256, 512],
                    "activation": "Tanh",
                    "gated": False,
                    "head_bias": False,
                },
                {
                    "condition_size": 1,
                    "input_size": 16,
                    "channels": 8,
                    "head_size": 1,
                    "kernel_size": 3,
                    "dilations": [1, 2, 4, 8, 16, 32, 64, 128, 256, 512],
                    "activation": "Tanh",
                    "gated": False,
                    "head_bias": True,
                },
            ],
            "head_scale": 0.02,
        },
        Architecture.LITE: {
            "layers_configs": [
                {
                    "input_size": 1,
                    "condition_size": 1,
                    "channels": 12,
                    "head_size": 6,
                    "kernel_size": 3,
                    "dilations": [1, 2, 4, 8, 16, 32, 64],
                    "activation": "Tanh",
                    "gated": False,
                    "head_bias": False,
                },
                {
                    "condition_size": 1,
                    "input_size": 12,
                    "channels": 6,
                    "head_size": 1,
                    "kernel_size": 3,
                    "dilations": [128, 256, 512, 1, 2, 4, 8, 16, 32, 64, 128, 256, 512],
                    "activation": "Tanh",
                    "gated": False,
                    "head_bias": True,
                },
            ],
            "head_scale": 0.02,
        },
        Architecture.FEATHER: {
            "layers_configs": [
                {
                    "input_size": 1,
                    "condition_size": 1,
                    "channels": 8,
                    "head_size": 4,
                    "kernel_size": 3,
                    "dilations": [1, 2, 4, 8, 16, 32, 64],
                    "activation": "Tanh",
                    "gated": False,
                    "head_bias": False,
                },
                {
                    "condition_size": 1,
                    "input_size": 8,
                    "channels": 4,
                    "head_size": 1,
                    "kernel_size": 3,
                    "dilations": [128, 256, 512, 1, 2, 4, 8, 16, 32, 64, 128, 256, 512],
                    "activation": "Tanh",
                    "gated": False,
                    "head_bias": True,
                },
            ],
            "head_scale": 0.02,
        },
    }[architecture]


def _get_configs(
    input_version: Version,
    input_basename: str,
    output_basename: str,
    delay: int,
    epochs: int,
    architecture: Architecture,
    ny: int,
    lr: float,
    lr_decay: float,
    batch_size: int,
):
    def get_kwargs():
        val_seconds = 9
        rate = REQUIRED_RATE
        if input_version.major == 1:
            train_val_split = -val_seconds * rate
            train_kwargs = {"stop": train_val_split}
            validation_kwargs = {"start": train_val_split}
        elif input_version.major == 2:
            blip_seconds = 1
            val_replicates = 2
            train_stop = -(blip_seconds + val_replicates * val_seconds) * rate
            validation_start = train_stop
            validation_stop = -(blip_seconds + val_seconds) * rate
            train_kwargs = {"stop": train_stop}
            validation_kwargs = {"start": validation_start, "stop": validation_stop}
        else:
            raise NotImplementedError(f"kwargs for input version {input_version}")
        return train_kwargs, validation_kwargs

    train_kwargs, validation_kwargs = get_kwargs()
    data_config = {
        "train": {"ny": ny, **train_kwargs},
        "validation": {"ny": None, **validation_kwargs},
        "common": {
            "x_path": input_basename,
            "y_path": output_basename,
            "delay": delay,
        },
    }
    model_config = {
        "net": {
            "name": "WaveNet",
            # This should do decently. If you really want a nice model, try turning up
            # "channels" in the first block and "input_size" in the second from 12 to 16.
            "config": _get_wavenet_config(architecture),
        },
        "loss": {"val_loss": "esr"},
        "optimizer": {"lr": lr},
        "lr_scheduler": {"class": "ExponentialLR", "kwargs": {"gamma": 1.0 - lr_decay}},
    }
    if torch.cuda.is_available():
        device_config = {"accelerator": "gpu", "devices": 1}
    elif torch.backends.mps.is_available():
        device_config = {"accelerator": "mps", "devices": 1}
    else:
        print("WARNING: No GPU was found. Training will be very slow!")
        device_config = {}
    learning_config = {
        "train_dataloader": {
            "batch_size": batch_size,
            "shuffle": True,
            "pin_memory": True,
            "drop_last": True,
            "num_workers": 0,
        },
        "val_dataloader": {},
        "trainer": {"max_epochs": epochs, **device_config},
    }
    return data_config, model_config, learning_config


def _esr(pred: torch.Tensor, target: torch.Tensor) -> float:
    return (
        torch.mean(torch.square(pred - target)).item()
        / torch.mean(torch.square(target)).item()
    )


def _plot(
    model,
    ds,
    window_start: Optional[int] = None,
    window_end: Optional[int] = None,
    filepath: Optional[str] = None,
    silent: bool = False,
):
    print("Plotting a comparison of your model with the target output...")
    with torch.no_grad():
        tx = len(ds.x) / 48_000
        print(f"Run (t={tx:.2f} sec)")
        t0 = time()
        output = model(ds.x).flatten().cpu().numpy()
        t1 = time()
        print(f"Took {t1 - t0:.2f} sec ({tx / (t1 - t0):.2f}x)")

    esr = _esr(torch.Tensor(output), ds.y)
    # Trying my best to put numbers to it...
    if esr < 0.01:
        esr_comment = "Great!"
    elif esr < 0.035:
        esr_comment = "Not bad!"
    elif esr < 0.1:
        esr_comment = "...This *might* sound ok!"
    elif esr < 0.3:
        esr_comment = "...This probably won't sound great :("
    else:
        esr_comment = "...Something seems to have gone wrong."
    print(f"Error-signal ratio = {esr:.4f}")
    print(esr_comment)

    plt.figure(figsize=(16, 5))
    plt.plot(output[window_start:window_end], label="Prediction")
    plt.plot(ds.y[window_start:window_end], linestyle="--", label="Target")
    plt.title(f"ESR={esr:.4f}")
    plt.legend()
    if filepath is not None:
        plt.savefig(filepath + ".png")
    if not silent:
        plt.show()


def train(
    input_path: str,
    output_path: str,
    train_path: str,
    input_version: Optional[Version] = None,
    epochs=100,
    delay=None,
    architecture: Union[Architecture, str] = Architecture.STANDARD,
    batch_size: int = 16,
    ny: int = 8192,
    lr=0.004,
    lr_decay=0.007,
    seed: Optional[int] = 0,
    save_plot: bool = False,
    silent: bool = False,
    modelname: str = "model",
):
    if seed is not None:
        torch.manual_seed(seed)

    if delay is None:
        if input_version is None:
            input_version = _detect_input_version(input_path)
        delay = _calibrate_delay(
            delay, input_version, input_path, output_path, silent=silent
        )
    else:
        print(f"Delay provided as {delay}; skip calibration")

    if not _check(input_path, output_path, input_version):
        print("Failed checks; exit training")
        return

    data_config, model_config, learning_config = _get_configs(
        input_version,
        input_path,
        output_path,
        delay,
        epochs,
        Architecture(architecture),
        ny,
        lr,
        lr_decay,
        batch_size,
    )

    print("Starting training. It's time to kick ass and chew bubblegum!")
    model = Model.init_from_config(model_config)
    data_config["common"]["nx"] = model.net.receptive_field
    dataset_train = init_dataset(data_config, Split.TRAIN)
    dataset_validation = init_dataset(data_config, Split.VALIDATION)
    train_dataloader = DataLoader(dataset_train, **learning_config["train_dataloader"])
    val_dataloader = DataLoader(dataset_validation, **learning_config["val_dataloader"])

    trainer = pl.Trainer(
        callbacks=[
            pl.callbacks.model_checkpoint.ModelCheckpoint(
                filename="checkpoint_best_{epoch:04d}_{step}_{ESR:.4f}_{MSE:.3e}",
                save_top_k=3,
                monitor="val_loss",
                every_n_epochs=1,
            ),
            pl.callbacks.model_checkpoint.ModelCheckpoint(
                filename="checkpoint_last_{epoch:04d}_{step}", every_n_epochs=1
            ),
        ],
        default_root_dir=train_path,
        **learning_config["trainer"],
    )
    trainer.fit(model, train_dataloader, val_dataloader)

    # Go to best checkpoint
    best_checkpoint = trainer.checkpoint_callback.best_model_path
    if best_checkpoint != "":
        model = Model.load_from_checkpoint(
            trainer.checkpoint_callback.best_model_path,
            **Model.parse_config(model_config),
        )
    model.cpu()
    model.eval()

    def window_kwargs(version: Version):
        if version.major == 1:
            return dict(
                window_start=100_000,  # Start of the plotting window, in samples
                window_end=101_000,  # End of the plotting window, in samples
            )
        elif version.major == 2:
            # Same validation set even though it's a different spot in the reamp file
            return dict(
                window_start=100_000,  # Start of the plotting window, in samples
                window_end=101_000,  # End of the plotting window, in samples
            )
        # Fallback:
        return dict(
            window_start=100_000,  # Start of the plotting window, in samples
            window_end=101_000,  # End of the plotting window, in samples
        )

    _plot(
        model,
        dataset_validation,
        filepath=train_path + "/" + modelname if save_plot else None,
        silent=silent,
        **window_kwargs(input_version),
    )
    return model
