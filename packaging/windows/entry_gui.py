import sys

from winkg.bootstrap import main


def check_torchvision_runtime() -> None:
    import torch
    from torchvision.ops import nms

    boxes = torch.tensor(
        [[0, 0, 10, 10], [1, 1, 11, 11], [20, 20, 30, 30]],
        dtype=torch.float32,
    )
    scores = torch.tensor([0.9, 0.8, 0.7])
    selected = nms(boxes, scores, 0.5).tolist()
    if selected != [0, 2]:
        raise RuntimeError(
            f"Torchvision NMS returned unexpected results: {selected}"
        )


if __name__ == "__main__":
    if sys.argv[1:] == ["--check-torchvision-runtime"]:
        check_torchvision_runtime()
        raise SystemExit(0)
    raise SystemExit(main())
