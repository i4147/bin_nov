#!/data/data/com.termux/files/usr/bin/env python
"""Build a photo mosaic from a folder of tile images.
Loads and resizes the tiles, matches each grid cell of the target image to the tile with the closest average colour and blends the tile layer with the target (``OVERLAY_STRENGTH``) before saving the mosaic.
The paths, grid size, tile size and blend strength are module constants.
Usage: python mosaic.py Examples: $ python mosaic.py Configure ``FOLDER_OF_IMAGES``, ``IMAGE_TO_MAKE`` and the grid constants before running."""

import os
from pathlib import Path

import cv2
import numpy as np


def load_tiles(tiles_folder, tile_size=(50, 50)):
    tiles = []
    tile_avg_colors = []
    valid_extensions = (".jpg", ".jpeg", ".png", ".bmp", ".webp")
    print("Loading and processing tile images...")
    for filename in os.listdir(tiles_folder):
        if filename.lower().endswith(valid_extensions):
            img_path = Path(tiles_folder).joinpath(filename)
            img = cv2.imread(img_path, cv2.IMREAD_COLOR)
            if img is not None:
                img_resized = cv2.resize(img, tile_size)
                tiles.append(img_resized)
                avg_color = img_resized.mean(axis=(0, 1))
                tile_avg_colors.append(avg_color)
    if not tiles:
        msg = f"No valid images found in folder: {tiles_folder}"
        raise ValueError(msg)
    print(f"Successfully loaded {len(tiles)} tile images.")
    return np.array(tiles), np.array(tile_avg_colors)


def create_mosaic_blended(target_image_path, tiles, tile_avg_colors, grid_size=(100, 100), alpha=0.3):
    target_img = cv2.imread(target_image_path, cv2.IMREAD_COLOR)
    if target_img is None:
        msg = f"Could not open target image: {target_image_path}"
        raise FileNotFoundError(msg)
    num_cols, num_rows = grid_size
    tile_h, tile_w, _ = tiles[0].shape
    new_w = num_cols * tile_w
    new_h = num_rows * tile_h
    target_resized = cv2.resize(target_img, (new_w, new_h))
    tile_layer = np.zeros((new_h, new_w, 3), dtype=np.uint8)
    print("Generating tile layout...")
    for i in range(num_rows):
        for j in range(num_cols):
            y1, y2 = i * tile_h, (i + 1) * tile_h
            x1, x2 = j * tile_w, (j + 1) * tile_w
            cell = target_resized[y1:y2, x1:x2]
            cell_avg = cell.mean(axis=(0, 1))
            distances = np.linalg.norm(tile_avg_colors - cell_avg, axis=1)
            best_match_idx = np.argmin(distances)
            tile_layer[y1:y2, x1:x2] = tiles[best_match_idx]
    print(f"Applying alpha blend overlay (Strength: {alpha})...")
    blended_mosaic = cv2.addWeighted(tile_layer, 1.0 - alpha, target_resized, alpha, 0)
    return blended_mosaic


if __name__ == "__main__":
    FOLDER_OF_IMAGES = "path/to/your/tile/folder"
    IMAGE_TO_MAKE = "path/to/your/target_image.jpg"
    OUTPUT_IMAGE = "mosaic_blended_result.png"
    GRID_ROWS_COLS = (120, 120)
    INDIVIDUAL_TILE_SIZE = (40, 40)
    OVERLAY_STRENGTH = 0.35
    try:
        tiles, tile_avgs = load_tiles(FOLDER_OF_IMAGES, tile_size=INDIVIDUAL_TILE_SIZE)
        result = create_mosaic_blended(
            IMAGE_TO_MAKE, tiles, tile_avgs, grid_size=GRID_ROWS_COLS, alpha=OVERLAY_STRENGTH
        )
        cv2.imwrite(OUTPUT_IMAGE, result)
        print(f"Success! Sharp blended mosaic saved as '{OUTPUT_IMAGE}'")
    except Exception as e:
        print(f"Error: {e}")
