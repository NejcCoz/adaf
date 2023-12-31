import glob
import logging
import os
import shutil
import time
from pathlib import Path
from time import localtime, strftime

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from aitlas.models import FasterRCNN, HRNet
from pyproj import CRS
from rasterio.features import shapes
from shapely.geometry import box, shape
from torch import cuda

import adaf.grid_tools as gt
from adaf.adaf_utils import (
    make_predictions_on_patches_object_detection,
    make_predictions_on_patches_segmentation,
    build_vrt_from_list,
    Logger
)
from adaf.adaf_vis import tiled_processing, image_tiling

logging.disable(logging.INFO)


def object_detection_vectors(predictions_dirs_dict, threshold=0.5, keep_ml_paths=False):
    """Converts object detection bounding boxes from text to vector format.

    Parameters
    ----------
    predictions_dirs_dict : dict
        Key is ML label, value is path to directory with results for that label.
    threshold : float
        Probability threshold for predictions.
    keep_ml_paths : bool
        If true, add path to ML predictions file from which the label was created as an attribute.

    Returns
    -------
    output_path : str
        Path to vector file.
    """
    # Use Path from pathlib
    path_to_predictions = Path(list(predictions_dirs_dict.values())[0])
    # Prepare output path (GPKG file in the data folder)
    output_path = path_to_predictions.parent / "object_detection.gpkg"

    appended_data = []
    crs = None
    for label, predicts_dir in predictions_dirs_dict.items():
        predicts_dir = Path(predicts_dir)
        file_list = list(predicts_dir.glob(f"*.txt"))

        for file_path in file_list:
            # Only read files that are not empty
            if not os.stat(file_path).st_size == 0:
                # Read predictions from TXT file
                data = pd.read_csv(file_path, sep=" ", header=None)
                data.columns = ["x0", "y0", "x1", "y1", "label", "score", "epsg", "res", "x_min", "y_max"]

                # EPSG code is added to every bbox, doesn't matter which we chose, it has to be the same for all entries
                if crs is None:
                    crs = CRS.from_epsg(int(data.epsg[0]))

                data.x0 = data.x_min + (data.res * data.x0)
                data.x1 = data.x_min + (data.res * data.x1)
                data.y0 = data.y_max - (data.res * data.y0)
                data.y1 = data.y_max - (data.res * data.y1)

                data["geometry"] = [box(*a) for a in zip(data.x0, data.y0, data.x1, data.y1)]
                data.drop(columns=["x0", "y0", "x1", "y1", "epsg", "res", "x_min", "y_max"], inplace=True)

                # Filter by probability threshold
                data = data[data['score'] > threshold]
                # Add paths to ML results
                if keep_ml_paths:
                    data["prediction_path"] = str(Path().joinpath(*file_path.parts[-3:]))
                # Don't append if there are no predictions left after filtering
                if data.shape[0] > 0:
                    appended_data.append(data)

    if appended_data:
        # We have at least one detection
        gdf = gpd.GeoDataFrame(pd.concat(appended_data, ignore_index=True), crs=crs)

        # # If same object from two different tiles overlap, join them into one
        # gdf["unique_i"] = gdf.index
        # to_concat = []
        # for label, _ in predictions_dirs_dict.items():
        #     gdf_1 = gdf[gdf["label"] == label]
        #     intersection_gdf = gdf_1.overlay(gdf_1, how="intersection", keep_geom_type=True)
        #     intersection_gdf = intersection_gdf.loc[intersection_gdf.unique_i_1 != intersection_gdf.unique_i_2]
        #     # The features to dissolve are the intersecting ones, excluding the self-intersections
        #     to_dissolve_gdf = gdf_1.loc[
        #         gdf_1.unique_i.isin(intersection_gdf.unique_i_1) | gdf_1.unique_i.isin(intersection_gdf.unique_i_2)
        #         ]
        #     to_dissolve_gdf_2 = to_dissolve_gdf.dissolve(by="label", aggfunc={"score": "mean"}).explode(
        #         index_parts=False).reset_index()
        #     # Other features should not be dissolved
        #     no_dissolve_gdf = gdf_1.loc[~gdf_1.index.isin(to_dissolve_gdf.index)].drop(columns=["unique_i"])
        #     # Compile
        #     result_gdf = pd.concat([to_dissolve_gdf_2, no_dissolve_gdf]).reset_index(drop=True)
        #     to_concat.append(result_gdf)
        # final_gdf = gpd.GeoDataFrame(pd.concat(to_concat).reset_index(drop=True), crs=gdf.crs)
        # # appended_data = appended_data.dissolve(by="label").explode(index_parts=False).reset_index(drop=False)

        # Export file
        gdf.to_file(str(output_path), driver="GPKG")
    else:
        output_path = ""

    return str(output_path)


def semantic_segmentation_vectors(predictions_dirs_dict, threshold=0.5,
                                  keep_ml_paths=False, roundness=None, min_area=None):
    """Converts semantic segmentation probability masks to polygons using a threshold. If more than one class, all
    predictions are stored in the same vector file, class is stored as label attribute.

    Parameters
    ----------
    predictions_dirs_dict : dict
        Key is ML label, value is path to directory with results for that label.
    threshold : float
        Probability threshold for predictions.
    keep_ml_paths : bool
        If true, add path to ML predictions file from which the label was created as an attribute.
    roundness : float
        Roundness threshold for post-processing. Remove features that fall below the threshold.
        For perfect circle roundness is 1, for square 0.785, and goes towards 0 for irregular shapes.
    min_area : float
        Minimum area threshold in m^2 (max = 40 m^2).

    Returns
    -------
    output_path : str
        Path to vector file.
    """
    # Prepare paths, use Path from pathlib (select one from dict, we only need parent)
    path_to_predictions = Path(list(predictions_dirs_dict.values())[0])
    # Output path (GPKG file in the data folder)
    output_path = path_to_predictions.parent / "semantic_segmentation.gpkg"

    gdf_out = []
    for label, predicts_dir in predictions_dirs_dict.items():
        predicts_dir = Path(predicts_dir)
        tif_list = list(predicts_dir.glob(f"*.tif"))

        # file = tif_list[4]

        for file in tif_list:
            with rasterio.open(file) as src:
                prob_mask = src.read()
                transform = src.transform
                crs = src.crs

                prediction = prob_mask.copy()

                # Mask probability map by threshold for extraction of polygons
                feature = prob_mask >= float(threshold)
                background = prob_mask < float(threshold)

                prediction[feature] = 1
                prediction[background] = 0

                # Outputs a list of (polygon, value) tuples
                output = list(shapes(prediction, transform=transform))

                # Find polygon covering valid data (value = 1) and transform to GDF friendly format
                poly = []
                for polygon, value in output:
                    if value == 1:
                        poly.append(shape(polygon))

            # If there is at least one polygon, convert to GeoDataFrame and append to list for output
            if poly:
                predicted_labels = gpd.GeoDataFrame(poly, columns=['geometry'], crs=crs)
                predicted_labels = predicted_labels.dissolve().explode(ignore_index=True)
                predicted_labels["label"] = label
                if keep_ml_paths:
                    predicted_labels["prediction_path"] = str(Path().joinpath(*file.parts[-3:]))
                gdf_out.append(predicted_labels)

    if gdf_out:
        # We have at least one detection
        gdf = gpd.GeoDataFrame(pd.concat(gdf_out, ignore_index=True), crs=crs)

        # # If same object from two different tiles overlap, join them into one
        # gdf["unique_i"] = gdf.index
        # to_concat = []
        # for label, _ in predictions_dirs_dict.items():
        #     gdf_1 = gdf[gdf["label"] == label]
        #     intersection_gdf = gdf_1.overlay(gdf_1, how="intersection", keep_geom_type=True)
        #     intersection_gdf = intersection_gdf.loc[intersection_gdf.unique_i_1 != intersection_gdf.unique_i_2]
        #     # The features to dissolve are the intersecting ones, excluding the self-intersections
        #     to_dissolve_gdf = gdf_1.loc[
        #         gdf_1.unique_i.isin(intersection_gdf.unique_i_1) | gdf_1.unique_i.isin(intersection_gdf.unique_i_2)
        #         ]
        #     to_dissolve_gdf_2 = to_dissolve_gdf.dissolve(by="label", aggfunc={"score": "mean"}).explode(
        #         index_parts=False).reset_index()
        #     # Other features should not be dissolved
        #     no_dissolve_gdf = gdf_1.loc[~gdf_1.index.isin(to_dissolve_gdf.index)].drop(columns=["unique_i"])
        #     # Compile
        #     result_gdf = pd.concat([to_dissolve_gdf_2, no_dissolve_gdf]).reset_index(drop=True)
        #     to_concat.append(result_gdf)
        # final_gdf = gpd.GeoDataFrame(pd.concat(to_concat).reset_index(drop=True), crs=gdf.crs)
        # # gdf_out = gdf_out.dissolve(by='label').explode(index_parts=False).reset_index(drop=False)

        # Post-processing
        if roundness:
            gdf["roundness"] = 4 * np.pi * gdf.geometry.area / (gdf.geometry.convex_hull.length ** 2)
            gdf = gdf[gdf["roundness"] > roundness]
        if min_area:
            gdf["area"] = gdf.geometry.area
            gdf = gdf[gdf["area"] > min_area]

        # Export file
        gdf.to_file(output_path.as_posix(), driver="GPKG")
    else:
        output_path = ""

    return str(output_path)


def run_visualisations(dem_path, tile_size, save_dir, nr_processes=1):
    """Calculates visualisations from DEM and saves them into VRT (Geotiff) file.

    Uses RVT (see adaf_vis.py).

    dem_path:
        Can be any raster file (GeoTIFF and VRT supported.)
    tile_size:
        In pixels
    save_dir:
        Save directory
    nr_processes:
        Number of processes for parallel computing

    """
    # Prepare paths
    in_file = Path(dem_path)

    # === STEP 1 ===
    # We need polygon covering valid data
    valid_data_outline = gt.poly_from_valid(
        in_file.as_posix(),
        save_gpkg=save_dir  # directory where *_validDataMask.gpkg will be stored
    )

    # === STEP 2 ===
    # Create reference grid, filter it and save it to disk
    tiles_extents = gt.bounding_grid(
        in_file.as_posix(),
        tile_size,
        tag=False
    )
    refgrid_name = in_file.as_posix()[:-4] + "_refgrid.gpkg"
    tiles_extents = gt.filter_by_outline(
        tiles_extents,
        valid_data_outline,
        save_gpkg=True,
        save_path=refgrid_name
    )

    # === STEP 3 ===
    # Run visualizations
    logging.debug("Start RVT vis")
    out_paths = tiled_processing(
        input_vrt_path=in_file.as_posix(),
        ext_list=tiles_extents,
        nr_processes=nr_processes,
        ll_dir=Path(save_dir)
    )

    # Remove reference grid and valid data mask files
    Path(valid_data_outline).unlink()
    Path(refgrid_name).unlink()

    return out_paths


def run_tiling(dem_path, tile_size, save_dir, nr_processes=1):
    """Calculates visualisations from DEM and saves them into VRT (Geotiff) file.

    Uses RVT (see adaf_vis.py).

    dem_path:
        Can be any raster file (GeoTIFF and VRT supported.)
    tile_size:
        In pixels
    save_dir:
        Save directory
    nr_processes:
        Number of processes for parallel computing

    """
    # Prepare paths
    in_file = Path(dem_path)

    # === STEP 1 ===
    # We need polygon covering valid data
    valid_data_outline = gt.poly_from_valid(
        in_file.as_posix(),
        save_gpkg=save_dir  # directory where *_validDataMask.gpkg will be stored
    )

    # === STEP 2 ===
    # Create reference grid, filter it and save it to disk
    tiles_extents = gt.bounding_grid(
        in_file.as_posix(),
        tile_size,
        tag=False
    )
    refgrid_name = in_file.as_posix()[:-4] + "_refgrid.gpkg"
    tiles_extents = gt.filter_by_outline(
        tiles_extents,
        valid_data_outline,
        save_gpkg=True,
        save_path=refgrid_name
    )

    # === STEP 3 ===
    # Run tiling
    logging.debug("Start RVT vis")
    out_paths = image_tiling(
        source_path=in_file.as_posix(),
        ext_list=tiles_extents,
        nr_processes=nr_processes,
        save_dir=Path(save_dir)
    )

    # Remove reference grid and valid data mask files
    Path(valid_data_outline).unlink()
    Path(refgrid_name).unlink()

    return out_paths


def run_aitlas_object_detection(labels, images_dir):
    """

    Parameters
    ----------
    labels
    images_dir

    Returns
    -------
    predictions_dirs: dict
        List of

    """
    images_dir = str(images_dir)

    # Paths to models are relative to the script path
    models = {
        "barrow": r".\ml_models\OD_barrow.tar",
        "enclosure": r".\ml_models\OD_enclosure.tar",
        "ringfort": r".\ml_models\OD_ringfort.tar",
        "AO": r".\ml_models\OD_AO.tar"
    }

    if cuda.is_available():
        logging.debug("> CUDA is available, running predictions on GPU!")
    else:
        logging.debug("> No CUDA detected, running predictions on CPU!")

    predictions_dirs = {}
    for label in labels:
        # Prepare the model
        model_config = {
            "num_classes": 2,  # Number of classes in the dataset
            "learning_rate": 0.0001,  # Learning rate for training
            "pretrained": True,  # Whether to use a pretrained model or not
            "use_cuda": cuda.is_available(),  # Set to True if you want to use GPU acceleration
            "metrics": ["map"]  # Evaluation metrics to be used
        }
        model = FasterRCNN(model_config)
        model.prepare()

        # Prepare path to the model
        model_path = models.get(label)
        # Path is relative to the Current script directory
        model_path = Path(__file__).resolve().parent / model_path
        # Load appropriate ADAF model
        model.load_model(model_path)
        logging.debug("Model successfully loaded.")

        preds_dir = make_predictions_on_patches_object_detection(
            model=model,
            label=label,
            patches_folder=images_dir
        )

        predictions_dirs[label] = preds_dir

    return predictions_dirs


def run_aitlas_segmentation(labels, images_dir):
    """

    Parameters
    ----------
    labels
    images_dir

    Returns
    -------
    predictions_dirs: dict
        List of

    """
    images_dir = str(images_dir)

    # Paths to models are relative to the script path
    models = {
        "barrow": r".\ml_models\barrow_HRNet_SLRM_512px_pretrained_train_12_val_124_with_Transformation.tar",
        "enclosure": r".\ml_models\enclosure_HRNet_SLRM_512px_pretrained_train_12_val_124_with_Transformation.tar",
        "ringfort": r".\ml_models\ringfort_HRNet_SLRM_512px_pretrained_train_12_val_124_with_Transformation.tar",
        "AO": r".\ml_models\AO_HRNet_SLRM_512px_pretrained_train_12_val_124_with_Transformation.tar"
    }

    if cuda.is_available():
        logging.debug("> CUDA is available, running predictions on GPU!")
    else:
        logging.debug("> No CUDA detected, running predictions on CPU!")

    predictions_dirs = {}
    for label in labels:
        # Prepare the model
        model_config = {
            "num_classes": 2,  # Number of classes in the dataset
            "learning_rate": 0.0001,  # Learning rate for training
            "pretrained": True,  # Whether to use a pretrained model or not
            "use_cuda": cuda.is_available(),  # Set to True if you want to use GPU acceleration
            "threshold": 0.5,
            "metrics": ["iou"]  # Evaluation metrics to be used
        }
        model = HRNet(model_config)
        model.prepare()

        logging.debug(label)

        # Prepare path to the model
        model_path = models.get(label)
        # Path is relative to the Current script directory
        model_path = Path(__file__).resolve().parent / model_path

        logging.debug(model_path)

        # Load appropriate ADAF model
        model.load_model(model_path)
        logging.debug("Model successfully loaded.")

        # Run inference
        preds_dir = make_predictions_on_patches_segmentation(
            model=model,
            label=label,
            patches_folder=images_dir
        )

        predictions_dirs[label] = preds_dir

    return predictions_dirs


def main_routine(inp):
    dem_path = Path(inp.dem_path)

    # Create unique name for results
    time_started = localtime()
    t0 = time.time()

    # Save results to parent folder of input file
    if inp.ml_type == "object detection":
        suff = "_obj"
    else:
        suff = "_seg"
    save_dir = Path(dem_path).parent / (dem_path.stem + strftime("_%Y%m%d_%H%M%S", time_started) + suff)
    save_dir.mkdir(parents=True, exist_ok=True)

    # Create logfile
    log_path = save_dir / "logfile.txt"
    logger = Logger(log_path, log_time=time_started)

    # --- VISUALIZATIONS ---
    logger.log_vis_inputs(dem_path, inp.vis_exist_ok)
    t1 = time.time()

    # Determine nr_processes from available CPUs (leave two free)
    my_cpus = os.cpu_count() - 2
    if my_cpus < 1:
        my_cpus = 1
    # The processing of the image is done on tiles (for better performance)
    tile_size_px = 1024  # Tile size has to be in base 2 (512, 1024) for inference to work!

    # vis_path is folder where visualizations are stored
    if inp.vis_exist_ok:
        # Create tiles (because image pix size has to be divisible by 32)
        out_paths = run_tiling(
            dem_path,
            tile_size_px,
            save_dir=save_dir.as_posix(),
            nr_processes=my_cpus
        )
    else:
        # Create visualisations
        out_paths = run_visualisations(
            dem_path,
            tile_size_px,
            save_dir=save_dir.as_posix(),
            nr_processes=my_cpus
        )

    vis_path = out_paths["output_directory"]
    vrt_path = out_paths["vrt_path"]

    t1 = time.time() - t1
    logger.log_vis_results(vis_path, vrt_path, inp.vis_exist_ok, t1)

    # Make sure it is a Path object!
    vis_path = Path(vis_path)

    # --- INFERENCE ---
    logger.log_inference_inputs(inp.ml_type, inp.ml_model_custom, inp.labels)
    # For logger
    save_raw = []
    t2 = time.time()
    if inp.ml_type == "object detection":
        logging.debug("Running object detection")
        predictions_dict = run_aitlas_object_detection(inp.labels, vis_path)

        vector_path = object_detection_vectors(predictions_dict, keep_ml_paths=inp.save_ml_output)
        if vector_path != "":
            logging.debug("Created vector file", vector_path)
        else:
            logging.debug("No archaeology detected")

        # Remove predictions files (bbox txt)
        if not inp.save_ml_output:
            for _, p_dir in predictions_dict.items():
                shutil.rmtree(p_dir)
        else:
            save_raw = [a for _, a in predictions_dict.items()]

    elif inp.ml_type == "segmentation":
        logging.debug("Running segmentation")
        predictions_dict = run_aitlas_segmentation(inp.labels, vis_path)

        vector_path = semantic_segmentation_vectors(
            predictions_dict,
            keep_ml_paths=inp.save_ml_output,
            roundness=inp.roundness,
            min_area=inp.min_area
        )
        if vector_path != "":
            logging.debug("Created vector file", vector_path)
        else:
            logging.debug("No archaeology detected")

        # Save predictions files (probability masks)
        if inp.save_ml_output:
            # Create VRT file for predictions
            for label, p_dir in predictions_dict.items():
                logging.debug("Creating vrt for", label)
                tif_list = glob.glob((Path(p_dir) / f"*{label}*.tif").as_posix())
                vrt_name = save_dir / (Path(p_dir).stem + ".vrt")
                build_vrt_from_list(tif_list, vrt_name)
                save_raw.append(vrt_name)
        else:
            for _, p_dir in predictions_dict.items():
                shutil.rmtree(p_dir)

    else:
        raise Exception("Wrong ml_type: choose 'object detection' or 'segmentation'")
    t2 = time.time() - t2

    logger.log_inference_results(vector_path, t2, save_raw)

    # Remove visualizations
    if not inp.save_vis:
        shutil.rmtree(vis_path)
        if vrt_path:
            Path(vrt_path).unlink()

    # TOTAL PROCESSING TIME
    t0 = time.time() - t0
    logger.log_total_time(t0)

    logging.debug("\n--\nFINISHED!")

    return vector_path


def batch_routine(inp):
    batch_list = inp.input_file_list

    if len(batch_list) == 1:
        logging.debug("Started SINGLE PROCESSING!")
    elif len(batch_list) > 1:
        logging.debug("Started BATCH PROCESSING!")
    else:
        logging.debug("NO FILES SELECTED!")

    for file in batch_list:
        logging.debug(" >>> ", file)
        inp.update(dem_path=file)

        main_routine(inp)

    return "ADAF FINISHED PROCESSING"
