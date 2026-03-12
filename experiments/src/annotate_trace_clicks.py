from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.image as mpimg


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Annotate click points on trace screenshots.")
    parser.add_argument("--trace-dir", required=True, help="Trace directory path")
    parser.add_argument("--out-dir", default="annotated", help="Output subdirectory name")
    parser.add_argument(
        "--include-scroll",
        action="store_true",
        help="Also annotate scroll actions on screenshots.",
    )
    parser.add_argument(
        "--include-hover",
        action="store_true",
        help="Also annotate hover actions on screenshots.",
    )
    return parser


def _draw_click_marker(image_path: Path, out_path: Path, x: int, y: int, step: int) -> None:
    img = mpimg.imread(str(image_path))
    h, w = img.shape[:2]

    fig = plt.figure(figsize=(w / 100, h / 100), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.imshow(img)
    ax.axis("off")

    ax.scatter([x], [y], s=180, c="red", edgecolors="white", linewidths=1.5, zorder=3)
    ax.text(
        x + 12,
        y - 12,
        f"step {step} ({x},{y})",
        color="white",
        fontsize=11,
        bbox={"facecolor": "black", "alpha": 0.75, "pad": 3},
        zorder=4,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=100)
    plt.close(fig)


def _draw_hover_marker(image_path: Path, out_path: Path, x: int, y: int, step: int) -> None:
    img = mpimg.imread(str(image_path))
    h, w = img.shape[:2]

    fig = plt.figure(figsize=(w / 100, h / 100), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.imshow(img)
    ax.axis("off")

    ax.scatter([x], [y], s=180, c="deepskyblue", edgecolors="white", linewidths=1.5, zorder=3)
    ax.text(
        x + 12,
        y - 12,
        f"step {step} hover ({x},{y})",
        color="white",
        fontsize=11,
        bbox={"facecolor": "navy", "alpha": 0.75, "pad": 3},
        zorder=4,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=100)
    plt.close(fig)


def _draw_scroll_marker(image_path: Path, out_path: Path, delta_y: int, step: int) -> None:
    img = mpimg.imread(str(image_path))
    h, w = img.shape[:2]

    fig = plt.figure(figsize=(w / 100, h / 100), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.imshow(img)
    ax.axis("off")

    x = w - 80
    y_top = max(60, h // 2 - 120)
    y_bottom = min(h - 60, h // 2 + 120)
    direction_up = delta_y < 0
    start_y = y_bottom if direction_up else y_top
    end_y = y_top if direction_up else y_bottom
    color = "gold"
    label = "scroll up" if direction_up else "scroll down"

    ax.annotate(
        "",
        xy=(x, end_y),
        xytext=(x, start_y),
        arrowprops={"arrowstyle": "->", "color": color, "lw": 4},
        zorder=4,
    )
    ax.text(
        x - 180,
        min(start_y, end_y) - 18,
        f"step {step} {label} ({delta_y})",
        color="black",
        fontsize=11,
        bbox={"facecolor": color, "alpha": 0.85, "pad": 3},
        zorder=5,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=100)
    plt.close(fig)


def _find_trace_dirs(root: Path) -> list[Path]:
    if (root / "steps.jsonl").exists():
        return [root]
    return sorted(path.parent for path in root.rglob("steps.jsonl"))


def _annotate_trace_dir(trace_dir: Path, out_dir_name: str, include_scroll: bool, include_hover: bool) -> tuple[int, dict[str, int], Path]:
    steps_path = trace_dir / "steps.jsonl"
    out_dir = trace_dir / out_dir_name
    out_dir.mkdir(parents=True, exist_ok=True)
    created = 0
    counts = {"click": 0, "hover": 0, "scroll": 0}

    with steps_path.open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            executed = row.get("executed", {})
            if not isinstance(executed, dict):
                continue

            action_type = str(executed.get("type", "")).strip()
            screenshot_name = str(row.get("screenshot", "")).strip()
            if not screenshot_name:
                continue
            image_path = trace_dir / screenshot_name
            if not image_path.exists():
                continue

            step = int(row.get("step", -1))
            stem = Path(screenshot_name).stem

            if action_type == "click":
                x = int(executed.get("x", -1))
                y = int(executed.get("y", -1))
                if x < 0 or y < 0:
                    continue
                _draw_click_marker(image_path, out_dir / f"{stem}_click.png", x, y, step)
                created += 1
                counts["click"] += 1
                continue

            if action_type == "hover" and include_hover:
                x = int(executed.get("x", -1))
                y = int(executed.get("y", -1))
                if x < 0 or y < 0:
                    continue
                _draw_hover_marker(image_path, out_dir / f"{stem}_hover.png", x, y, step)
                created += 1
                counts["hover"] += 1
                continue

            if action_type == "scroll" and include_scroll:
                delta_y = int(executed.get("delta_y", 0))
                if delta_y == 0:
                    continue
                _draw_scroll_marker(image_path, out_dir / f"{stem}_scroll.png", delta_y, step)
                created += 1
                counts["scroll"] += 1

    return created, counts, out_dir


def main() -> int:
    args = _build_parser().parse_args()
    trace_dir = Path(args.trace_dir).resolve()
    trace_dirs = _find_trace_dirs(trace_dir)
    if not trace_dirs:
        raise FileNotFoundError(f"No trace directories with steps.jsonl found under: {trace_dir}")

    total_created = 0
    total_counts = {"click": 0, "hover": 0, "scroll": 0}
    for one_trace_dir in trace_dirs:
        created, counts, out_dir = _annotate_trace_dir(
            one_trace_dir,
            args.out_dir,
            include_scroll=args.include_scroll,
            include_hover=args.include_hover,
        )
        total_created += created
        for key, value in counts.items():
            total_counts[key] += value
        print(
            f"{one_trace_dir}: created={created} "
            f"(click={counts['click']}, hover={counts['hover']}, scroll={counts['scroll']}) "
            f"-> {out_dir}"
        )

    print(f"Annotated images total: {total_created}")
    print(
        "Breakdown: "
        f"click={total_counts['click']}, "
        f"hover={total_counts['hover']}, "
        f"scroll={total_counts['scroll']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
