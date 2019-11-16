"""This module contains utilities for data I/O and generating training data."""

import numpy as np
import tensorflow as tf
import imgaug as ia
import imgaug.augmenters as iaa

from typing import Union, List, Tuple

ArrayLike = Union[np.ndarray, tf.Tensor]


def make_image_dataset(images: Union[ArrayLike, List[ArrayLike]]) -> tf.data.Dataset:
    """Creates a tf.data.Dataset of images.

    Args:
        images: The image data specified as either a single rank-4 array of shape
            (n_samples, height, width, channels), or a list length n_samples whose
            elements are rank-3 arrays of shape (height, width, channels). The latter is
            necessary when the images are of variable heights/widths but note that they
            must be of the same dtype.

    Returns:
        A tf.data.Dataset with n_samples elements, where each element is a rank-3
        tf.Tensor of the same height, width, channels and dtype as the input images.
    """

    if isinstance(images, list):
        return tf.data.Dataset.from_generator(
            lambda: images, output_types=images[0].dtype
        )
    elif isinstance(images, (np.ndarray, tf.Tensor)):
        return tf.data.Dataset.from_tensor_slices(images)
    else:
        raise ValueError("Invalid image type provided.")


def make_points_dataset(points: Union[ArrayLike, List[ArrayLike]]) -> tf.data.Dataset:
    """Creates a tf.data.Dataset of points.

    Args:
        points: The coordinates associated with a set of landmarks, specified as an
            array of shape (n_samples, n_instances, n_nodes, 2), a list of length
            n_samples whose elements are arrays of shape (n_instances, n_nodes, 2), or a
            list of length n_samples of arrays of shape (n_nodes, 2) if this is a single
            instance dataset. The n_nodes are the number of unique landmarks. The
            elements of the last axis are the x and y coordinates on the corresponding
            image, at the same scale/resolution as that image. Points that are missing
            or not visible points can be denoted by NaNs for both the x and y
            coordinates.

    Returns:
        A tf.data.Dataset with n_samples elements, where each element is a tf.Tensor of
        shape (n_instances, n_nodes, 2). If a list of rank-2 arrays was provided, the
        points will be promoted to rank-3 by prepending a singleton dimension as the
        first axis.
    """

    if isinstance(points, list):
        if points[0].ndim == 2:
            return tf.data.Dataset.from_generator(
                lambda: (tf.expand_dims(p, 0) for p in points),
                output_types=points[0].dtype,
            )

        elif points[0].ndim == 3:
            return tf.data.Dataset.from_generator(
                lambda: points, output_types=points[0].dtype
            )

        else:
            raise ValueError(
                "Invalid point type provided. Elements must be rank-2 or 3."
            )

    elif isinstance(points, (np.ndarray, tf.Tensor)):
        return tf.tensor_slices(points)

    else:
        raise ValueError("Invalid points type provided.")


def augment_dataset(
    ds_images: tf.data.Dataset,
    ds_points: tf.data.Dataset,
    rotate: bool = True,
    rotation_min_angle: float = -180,
    rotation_max_angle: float = 180,
    scale: bool = False,
    scale_min: float = 0.9,
    scale_max: float = 1.1,
    uniform_noise: bool = False,
    min_noise_val: float = 0.0,
    max_noise_val: float = 0.1,
    gaussian_noise: bool = False,
    gaussian_noise_mean: float = 0.05,
    gaussian_noise_stddev: float = 0.02,
) -> tf.data.Dataset:
    """Augments a pair of image and points dataset.

    Args:
        ds_images: tf.data.Dataset
        ds_points: tf.data.Dataset
        rotate: bool = True
        rotation_min_angle: float = -180
        scale: bool = False
        scale_min: float = 0.9
        scale_max: float = 1.1
        rotation_max_angle: float = 180
        uniform_noise: bool = True
        min_noise_val: float = 0.
        max_noise_val: float = 0.1
        gaussian_noise: bool = False,
        gaussian_noise_mean: float = 0.05
        gaussian_noise_stddev: float = 0.02

    Returns:
        ds_aug: tf.data.Dataset
    """

    # Setup augmenter.
    aug_stack = []
    if rotate:
        aug_stack.append(iaa.Affine(rotate=(-rotation_min_angle, rotation_max_angle)))

    if scale:
        aug_stack.append(iaa.Affine(scale=(scale_min, scale_max)))

    if uniform_noise:
        aug_stack.append(iaa.AddElementwise(value=(min_noise_val, max_noise_val)))

    if gaussian_noise:
        aug_stack.append(
            iaa.AdditiveGaussianNoise(
                loc=gaussian_noise_mean, scale=gaussian_noise_stddev
            )
        )

    aug = iaa.Sequential(aug_stack)

    # Define augmentation function to map over each sample.
    def aug_fn(img, pt):
        aug_det = aug.to_deterministic()
        kps = ia.KeypointsOnImage.from_xy_array(pt.numpy(), tuple(img.shape))

        img = aug_det.augment_image(img.numpy())
        pt = aug_det.augment_keypoints(kps).to_xy_array()

        return img, pt

    # Zip both streams
    ds_img_and_pts = tf.data.Dataset.zip((ds_images, ds_points))

    # Augment
    ds_aug = ds_img_and_pts.map(
        lambda img, pt: tf.py_function(
            func=aug_fn, inp=[img, pt], Tout=[tf.uint8, tf.float32]
        ),
        num_parallel_calls=tf.data.experimental.AUTOTUNE,
    )

    return ds_aug


def get_bbox_centroid(points: tf.Tensor) -> tf.Tensor:
    """Returns the centroid of a bounding box around points.

    Args:
        points: Tensor of shape (n_instances, n_nodes, 2) representing the x and y
            coordinates of instances within a frame. Missing or not visible points
            should be denoted by NaNs.

    Returns:
        centroids: Tensor of shape (n_instances, 2) representing the center of
        the bounding boxes formed by all the points per instance.

    Notes:
        NaNs will be ignored in centroid computation.
    """

    # Mask so we ignore NaNs appropriately.
    masked_pts = tf.ragged.boolean_mask(points, tf.math.is_finite(points))

    # Compute bbox centroid from bounds.
    pts_min = tf.reduce_min(masked_pts, axis=1)
    pts_max = tf.reduce_max(masked_pts, axis=1)
    centroids = 0.5 * (pts_max + pts_min)

    return centroids.to_tensor()


def get_bbox_centroid_from_node_ind(points: tf.Tensor, node_ind: int) -> tf.Tensor:
    """Returns the centroid of a bounding box from a node index.

    This function is useful when using a specific landmark type as the centroid for
    centering instances.

    Args:
        points: Tensor of shape (n_instances, n_nodes, 2) representing the x and y
            coordinates of instances within a frame. Missing or not visible points
            should be denoted by NaNs.
        node_ind: Scalar int indexing into axis 1 of points.

    Returns:
        centroids: Tensor of shape (n_instances, 2) representing the center of the
        bounding boxes formed by all the points per instance. If the point for node_ind
        is present, this function is equivalent to indexing into that point. If not, the
        centroid is computed from the bounding box of all visible points.
    """

    centroids = tf.gather(points, node_ind, axis=1)
    all_pts_centroids = get_bbox_centroid(points)
    centroids = tf.where(tf.math.is_nan(centroids), all_pts_centroids, centroids)

    return centroids


def get_centered_bboxes(
    centroids: tf.Tensor, box_width: int, box_height: int
) -> tf.Tensor:
    """Compute centered bounding boxes from centroids and box dimensions.

    Args:
        centroids: Tensor of shape (n_instances, 2) representing the center of
            the bounding boxes.
        box_width: Scalar int specifying the width of the bounding box.
        box_height: Scalar int specifying the height of the bounding box.

    Returns:
        bboxes: Tensor of shape (n_instances, 4) in the format [y1, x1, y2, x2] in
        absolute image coordinates.
    """

    # [[y1, x1, y2, x2]]
    bbox_delta = tf.constant(
        [[-0.5 * box_height, -0.5 * box_width, 0.5 * box_height, 0.5 * box_width]]
    )
    bboxes = tf.tile(tf.reverse(centroids, axis=[-1]), [1, 2]) + bbox_delta
    return bboxes


def normalize_bboxes(bboxes: tf.Tensor, img_height: int, img_width: int) -> tf.Tensor:
    """Normalize bboxes from absolute to relative coords.

    This function is useful for computing the expected representation for TensorFlow
    image cropping functions.

    Args:
        bboxes: Tensor of shape (n_boxes, 4) in the format [y1, x1, y2, x2] in
            absolute image coordinates.
        img_height: Scalar int specifying the height of the full image.
        img_width: Scalar int specifying the width of the full image.
    
    Returns:
        normalized_bboxes: Tensor of shape (n_boxes, 4) in the format [y1, x1, y2, x2]
        in normalized image coordinates. These values will be in the range [0, 1],
        computed by dividing x coordinates by (img_width - 1) and y coordinates by
        [img_height - 1].
    """
    img_bounds_norm = tf.cast(
        [[img_height - 1, img_width - 1, img_height - 1, img_width - 1]], tf.float32
    )
    normalized_bboxes = bboxes / img_bounds_norm
    return normalized_bboxes


def get_bbox_offsets(bboxes: tf.Tensor) -> tf.Tensor:
    """Returns the top-left xy coordinates of bboxes."""
    return tf.reverse(bboxes[:, :2], axis=[-1])


def pts_to_bbox(points: tf.Tensor, bboxes: tf.Tensor) -> tf.Tensor:
    """Translates points from absolute image to bounding box coordinates.

    Args:
        points: Tensor of shape (n_instances, n_nodes, 2) representing the x and y
            coordinates of instances within a frame.
        bboxes: Tensor of shape (n_boxes, 4) in the format [y1, x1, y2, x2] in absolute
            image coordinates.
    
    Returns:
        box_points: Tensor of shape (n_boxes, n_instances, n_nodes, 2) representing the
        x and y coordinates of each instance within each box.
    """
    offsets = get_bbox_offsets(bboxes)  # (n_boxes, xy)
    points = tf.expand_dims(points, axis=0)  # (1, n_instances, n_nodes, xy)
    return points - tf.reshape(
        offsets, [-1, 1, 1, 2]
    )  # (n_boxes, n_instances, n_nodes, xy)


def crop_bboxes(
    image: tf.Tensor,
    bboxes: tf.Tensor,
    box_width: int,
    box_height: int,
    normalize_image: bool = True,
) -> tf.Tensor:
    """Crop bounding boxes within an image.

    Args:
        image: A rank-3 tensor of shape (height, width, channels) of dtype float32 or
            uint8. See notes below about normalization if the image is uint8.
        bboxes: Tensor of shape (n_boxes, 4) in the format [y1, x1, y2, x2] in
            absolute image coordinates.
        box_width: Scalar int specifying the width of the bounding box. If this
            is not the same as the width of the coordinates in bboxes, the image patch
            will be resized to this width.
        box_height: Scalar int specifying the height of the bounding box. If this
            is not the same as the height of the coordinates in bboxes, the image patch
            will be resized to this height.
        normalize_image: If True, the cropped patches will be divided by 255. This 
            parameter is useful if the input image is of dtype uint8 since the output
            after cropping is automatically cast to float32. Set to False if the input
            image is already in the range [0, 1].
      
    Returns:
        image_boxes: The patches containing the cropped boxes defined by bboxes as a
        tensor of shape (n_boxes, box_height, box_width, channels) and dtype float32.

        If normalize_image was True, the output is divided by 255 to normalize uint8
        images to the range [0, 1].

    Notes:
        If the images are loaded as uint8, it is memory efficient to keep the full
        resolution images in that dtype and just cast the smaller patches to float32.
    """

    # Normalize bboxes to relative coordinates in [0, 1].
    img_height, img_width, _ = image.shape
    normalized_bboxes = normalize_bboxes(bboxes, img_height, img_width)

    # Crop.
    box_indices = tf.zeros_like(bboxes[:, 0], dtype=tf.int32)
    image_boxes = tf.image.crop_and_resize(
        tf.expand_dims(image, axis=0),
        boxes=normalized_bboxes,
        box_indices=box_indices,
        crop_size=[box_height, box_width],
        method="bilinear",
    )

    if normalize_image:
        # Normalize range.
        image_boxes /= 255.0

    return image_boxes


def instance_crop(
    image: tf.Tensor,
    points: tf.Tensor,
    box_height: int,
    box_width: int,
    use_ctr_node: bool = False,
    ctr_node_ind: int = 0,
    normalize_image: bool = True,
) -> tf.Tensor:
    """Crops an image around the instances in pts.

    This function serves as a convenience wrapper around low level processing functions
    that enable centroid-based cropping. It is a useful target for tf.data.Dataset.map.

    Args:
        image: A rank-3 tensor of shape (height, width, channels) of dtype float32 or
            uint8. See notes below about normalization if the image is uint8.
        points: Tensor of shape (n_instances, n_nodes, 2) representing the x and y
            coordinates of instances within a frame. Missing or not visible points
            should be denoted by NaNs.
        box_width: Scalar int specifying the width of the bounding box.
        box_height: Scalar int specifying the height of the bounding box.
        use_ctr_node: If True, the coordinate of the node specified by ctr_node_ind will
            be used as the centroid whenever it is visible.
        ctr_node_ind: Scalar int indexing into axis 1 of points.
        normalize_image: If True, the cropped patches will be divided by 255. This
            parameter is useful if the input image is of dtype uint8 since the output
            after cropping is automatically cast to float32. Set to False if the input
            image is already in the range [0, 1].

    Returns:
        Tuple of (instance_images, instance_points, instance_ctr_points).

        instance_images: Tensor of shape (n_instances, box_size, box_size, channels),
        float32 and scaled to [0, 1]. If image is of dtype uint8, normalized_image must
        be specified to appropriately scale to this range after cropping.

        instance_points: Tensor of shape (n_instances, n_instances, n_nodes, 2), where
        the first axis corresponds to the points mapped to the first crop in
        instance_images.

        instance_ctr_points: Tensor of shape (n_instances, n_nodes, 2) containing just
        the points for the centered instance in each image. This is useful when there
        are multiple instances in the image as it convenient when applying further data
        transformations exclusively to the centered instance.
  """

    if use_ctr_node:
        centroids = get_bbox_centroid_from_node_ind(points, ctr_node_ind)
    else:
        centroids = get_bbox_centroid(points)
        instance_bboxes = get_centered_bboxes(centroids, (box_height, box_width))
        instance_points = pts_to_bbox(points, instance_bboxes)
        instance_images = crop_bboxes(image, instance_bboxes, (box_height, box_width))

    # Pull out the "diagonal" of instance_points.
    ctr_inds = ctr_inds = tf.tile(
        tf.expand_dims(tf.range(tf.shape(instance_points)[0]), 1), [1, 2]
    )
    instance_ctr_points = tf.gather_nd(instance_points, ctr_inds)

    return instance_images, instance_points, instance_ctr_points


def make_confmaps(
    points: tf.Tensor,
    image_height: int,
    image_width: int,
    output_scale: float = 1.0,
    sigma: float = 3.0,
) -> tf.Tensor:
    """Generates confidence maps from pts.

    Args:
        points: Tensor with shape (n_instances, channels, 2) with the last axis in xy
            format. Points with NaNs will generate channels with all 0 in the
            corresponding slice of the output.
        image_height: scalar height of the image that the points are on.
        image_width: scalar width of the image that the points are on.
        output_scale: Relative scaling of the output confmaps.
        sigma: Gaussian kernel width around each point.

    Returns:
        confmaps: rank-4 (n, out_height, out_width, channels)
        out_height = height * scale
        out_width = width * scale
    """

    # Generate scaled sampling grid.
    yv = tf.range(0, image_height, 1.0 / output_scale, dtype=tf.float32)
    xv = tf.range(0, image_width, 1.0 / output_scale, dtype=tf.float32)

    # Splits [n, c, 2] -> ([n, c, 1], [n, c, 1]).
    x, y = tf.split(points, 2, axis=-1)

    # Reshape into [n, 1, 1, c].
    x = tf.squeeze(tf.expand_dims(tf.expand_dims(x, 1), 1), axis=-1)
    y = tf.squeeze(tf.expand_dims(tf.expand_dims(y, 1), 1), axis=-1)

    # Generate confmaps with broadcasting over height/width.
    confmaps = tf.exp(
        -(
            (tf.reshape(xv, [1, 1, -1, 1]) - x) ** 2
            + (tf.reshape(yv, [1, -1, 1, 1]) - y) ** 2
        )
        / (2 * sigma ** 2)
    )

    # Replace NaNs with 0 for channels with missing peaks.
    confmaps = tf.where(tf.math.is_nan(confmaps), 0.0, confmaps)

    return confmaps


def make_pafs(
    points: tf.Tensor,
    edges: np.ndarray,
    image_height: int,
    image_width: int,
    output_scale: float = 1.0,
    distance_threshold: float = 3.0,
) -> tf.Tensor:
    """Generate part affinity fields from points and edges.

    Args:
        points: Tensor with shape (n_instances, channels, 2) with the last axis in xy
            format. Points with NaNs will effectively not generate PAFs for edges that
            include them (for the instances that have them missing).
        edges: Array of shape (n_edges, 2) and dtype int where each row defines the
            (src_node_ind, dst_node_ind) that index into axis 1 of points to create the
            directed edges of the PAFs.
        image_height: Width of the full scale image that the points are on.
        image_width: Height of the full scale image that the points are on.
        output_scale: Relative scale (size) of the output PAF tensor.
        distance_threshold: Determines how far way from the edge lines that the PAF
            vectors should be defined (in full scale absolute image units). Increase
            this if the PAFs need to have increased support on the image (e.g., for 
            thicker limbs).

    Returns:
        pafs: A tf.Tensor of shape (paf_height, paf_width, 2 * n_edges). If multiple
        instances were present in the points array, their PAFs are added together.
    """

    # Pull out source and destination points points for each edge.
    # (n_instances, n_edges, 2 [src, dst], 2 [x, y])
    edge_points = tf.gather(points, edges, axis=1)

    # Compute displacement of dest points relative to source.
    # (n_instances, n_edges, 2)
    delta_points = edge_points[:, :, 1, :] - edge_points[:, :, 0, :]

    # Compute the Euclidean edge vector lengths.
    # (n_instances, n_edges, 1)
    edge_lengths = tf.linalg.norm(delta_points, axis=-1, keepdims=True)

    # Compute unit vectors parallel to the edge line, pointing from source to dest.
    # (n_instances, n_edges, 2 [x, y])
    unit_vectors = delta_points / edge_lengths

    # Compute unit vectors perpendicular to the edge lines.
    # (n_instances, n_edges, 2 [x, y])
    # perpendicular_unit_vectors = tf.reverse(unit_vectors, axis=[-1]) *
    #     tf.constant([[[-1., 1.]]])
    perpendicular_unit_vectors = tf.stack(
        [-unit_vectors[:, :, 1], unit_vectors[:, :, 0]], axis=-1
    )

    # Create sampling grid vectors and broadcast to full grid shape.
    yv = tf.reshape(
        tf.range(0, image_height, 1.0 / output_scale, dtype=tf.float32), [1, 1, -1, 1]
    )
    xv = tf.reshape(
        tf.range(0, image_width, 1.0 / output_scale, dtype=tf.float32), [1, -1, 1, 1]
    )
    grid_shape = tf.reduce_max(tf.stack([xv.shape, yv.shape], axis=0), axis=0)

    # Generate sampling grid.
    # (1, height, width, 1, 2 [x, y])
    sampling_grid = tf.stack(
        [tf.broadcast_to(xv, grid_shape), tf.broadcast_to(yv, grid_shape)], axis=-1
    )

    # Expand source points for broadcasting.
    # (n_instances, 1, 1, n_edges, 2 [x, y])
    source_points = tf.expand_dims(tf.expand_dims(edge_points[:, :, 0, :], 1), 1)

    # Translate grid to have an origin at the source point.
    # (n_instances, height, width, n_edges, 2 [x, y])
    source_relative_grid = sampling_grid - source_points

    # Compute signed distance along edge vector.
    # (n_instances, height, width, n_edges, 1
    parallel_distance = tf.reduce_sum(
        (tf.expand_dims(tf.expand_dims(unit_vectors, 1), 1) * source_relative_grid),
        axis=-1,
        keepdims=True,
    )

    # Compute absolute distance perpendicular to the edge vector.
    # (n_instances, height, width, n_edges, 1)
    perpendicular_distance = tf.abs(
        tf.reduce_sum(
            (
                tf.expand_dims(tf.expand_dims(perpendicular_unit_vectors, 1), 1)
                * source_relative_grid
            ),
            axis=-1,
            keepdims=True,
        )
    )

    # Create binary mask over pixels that each edge PAF should be defined in based on
    # parallel and perpendicular distances.
    after_edge_source = parallel_distance >= -distance_threshold
    before_edge_dest = parallel_distance <= (
        distance_threshold + tf.expand_dims(tf.expand_dims(edge_lengths, 1), 1)
    )
    within_edge_width = perpendicular_distance <= distance_threshold

    # Final PAF mask is the combination of all criteria.
    # (n_instances, height, width, n_edges, 1)
    paf_mask = after_edge_source & before_edge_dest & within_edge_width

    # Create the PAF by applying the unit vectors at the masked grid locations.
    # (n_instances, height, width, n_edges, 2)
    pafs = tf.cast(paf_mask, tf.float32) * tf.expand_dims(
        tf.expand_dims(unit_vectors, 1), 1
    )

    # Deal with NaNs for edges involving missing points.
    pafs = tf.where(tf.math.is_nan(pafs), 0.0, pafs)

    # Reduce over instances.
    # (height, width, n_edges, 2)
    pafs = tf.reduce_sum(pafs, axis=0)

    # Flatten xy axis.
    # (height, width, 2 * n_edges)
    pafs = tf.reshape(pafs, list(pafs.shape[:-2]) + [-1])

    return pafs


def make_confmap_ds(
    ds_img_and_pts: tf.data.Dataset, sigma: float = 3.0, output_scale: float = 1.0
) -> tf.data.Dataset:
    """Creates a confmaps dataset with confmaps from all points.

    Args:
        ds_img_and_pts: A tf.data.Dataset that generates tuples of (image, points).
        sigma: Gaussian kernel width around each point.
        output_scale: Relative scaling of the output confmaps.

    Returns:
        ds_cms: A tf.data.Dataset that returns elements that are tuples of
        (image, confmaps), where confmaps contains the confidence maps generated from
        all points. If more than one instance is present, they are max-reduced by node.
    """

    def gen_cm_fn(img, pts):

        # Instance-wise confmaps of shape (n_instances, height, width, n_nodes).
        instance_cms = make_confmaps(
            pts,
            tf.shape(img)[0],
            tf.shape(img)[1],
            output_scale=output_scale,
            sigma=sigma,
        )

        # Confmaps with peaks from all instances.
        # (height, width, n_nodes)
        cms_all = tf.reduce_max(instance_cms, axis=0)

        return img, cms_all

    ds_cms = ds_img_and_pts.map(
        gen_cm_fn, num_parallel_calls=tf.data.experimental.AUTOTUNE
    )

    return ds_cms


def make_instance_confmap_ds(
    ds_img_and_pts: tf.data.Dataset,
    sigma: float = 3.0,
    output_scale: float = 1.0,
    with_instance_cms: bool = False,
    with_all_peaks: bool = True,
    with_ctr_peaks: bool = False,
) -> tf.data.Dataset:
    """Creates a confmaps dataset with optionally instance-wise confmaps.

    This function is useful to create datasets for top-down supervision on
    instance-centered models.

    Args:
        ds_img_and_pts: A tf.data.Dataset that generates tuples of
            (image, points, ctr_points), where the last element are the points just for
            the centered instance. This can be produced as the output of instance_crop.
        sigma: Gaussian kernel width around each point.
        output_scale: Relative scaling of the output confmaps.
        with_instance_cms: If True, the full (n_instances, height, width, n_nodes)
            confmaps will be returned.
        with_all_peaks: If True, confmaps will be reduced over n_instances to produce
            confmaps of shape (height, width, n_nodes) with peaks from all instances.
        with_ctr_peaks: If True, confmaps will be generated from the ctr_points to
            produce confmaps of shape (height, width, n_nodes) with peaks from just the
            centered instances.

    Returns:
        ds_cms: A tf.data.Dataset that returns elements that are tuples of
        (image, (instance_cms, cms_all, cms_ctr)) or some subset of the specified
        confmaps depending on the outputs requested.
    """

    def gen_cm_fn(img, pts, ctr_pts):

        outputs = []
        if with_instance_cms or with_all_peaks:
            # Instance-wise confmaps of shape (n_instances, height, width, n_nodes).
            instance_cms = make_confmaps(
                pts,
                tf.shape(img)[0],
                tf.shape(img)[1],
                output_scale=output_scale,
                sigma=sigma,
            )

            if with_instance_cms:
                outputs.append(instance_cms)

            if with_all_peaks:
                # Confmaps with peaks from all instances.
                # (height, width, n_nodes)
                outputs.append(tf.reduce_max(instance_cms, axis=0))

        if with_ctr_peaks:
            # Confmaps with peaks from only the center instance.
            # (height, width, n_nodes)
            outputs.append(
                tf.reduce_max(
                    make_confmaps(
                        tf.expand_dims(ctr_pts, axis=0),
                        tf.shape(img)[0],
                        tf.shape(img)[1],
                        output_scale=output_scale,
                        sigma=sigma,
                    ),
                    axis=0,
                )
            )

        return img, tuple(outputs)

    ds_cms = ds_img_and_pts.map(
        gen_cm_fn, num_parallel_calls=tf.data.experimental.AUTOTUNE
    )

    return ds_cms


def make_paf_ds(
    ds_img_and_pts: tf.data.Dataset,
    edges: np.ndarray,
    output_scale: float = 1.0,
    distance_threshold: float = 3.0,
) -> tf.data.Dataset:
    """Creates a confmaps dataset with confmaps from all points.

    Args:
        ds_img_and_pts: A tf.data.Dataset that generates tuples of (image, points).
        edges: Array of shape (n_edges, 2) and dtype int where each row defines the
            (src_node_ind, dst_node_ind) that index into axis 1 of points to create the
            directed edges of the PAFs.
        output_scale: Relative scale (size) of the output PAF tensor.
        distance_threshold: Determines how far way from the edge lines that the PAF
            vectors should be defined (in full scale absolute image units). Increase
            this if the PAFs need to have increased support on the image (e.g., for 
            thicker limbs).

    Returns:
        ds_pafs: A tf.data.Dataset that returns elements that are tuples of
        (image, pafs), where pafs contains the generated PAFs.
    """

    def gen_paf_fn(img, pts, ctr_pts=None):

        pafs = make_pafs(
            pts,
            edges,
            image_height=tf.shape(img)[0],
            image_width=tf.shape(img)[1],
            output_scale=output_scale,
            distance_threshold=distance_threshold,
        )

        return img, pafs

    ds_pafs = ds_img_and_pts.map(
        gen_paf_fn, num_parallel_calls=tf.data.experimental.AUTOTUNE
    )

    return ds_pafs