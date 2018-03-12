""" Helper functions describing pipelines for creating large samples of nodules """

from copy import copy
import PIL
from ..dataset import Pipeline, F  # pylint: disable=no-name-in-module

# global constants defining args of some actions in pipeline
SPACING = (1.7, 1.0, 1.0)  # spacing of scans after spacing unification
SHAPE = (400, 512, 512)  # shape of scans after spacing unification
RESIZE_FILTER = PIL.Image.LANCZOS  # high-quality filter of resize
PADDING = 'reflect'  # padding-mode that produces the least amount of artefacts
METHOD = 'pil-simd'  # robust resize-engine
kwargs_default = dict(shape=SHAPE, spacing=SPACING, resample=RESIZE_FILTER, padding=PADDING, method=METHOD)

# define the number of times each cancerous nodule is dumped.
# with this number of iterations, the whole luna-dataset will
# produce approximately 115000 cancerous crops
N_ITERS = 100  # N_ITERS * (num_luna_nodules=1149) ~ 115000

# these params ensure that the number of non-cancerous crops will also
# be around 115000 (when run on the whole luna-dataset)
RUN_BATCH_SIZE = 8
NON_CANCER_BATCH_SIZE = 1030  # NON_CANCER_BATCH_SIZE * (len_of_lunaset=888) / RUN_BATCH_SIZE ~ 115000


def get_crops(nodules, fmt='raw', nodule_shape=(32, 64, 64), batch_size=20, share=0.5, histo=None,
              variance=(36, 144, 144), hu_lims=(-1000, 400), **kwargs):
    """ Get pipeline that performs preprocessing and crops cancerous/non-cancerous nodules in
    a chosen proportion.

    Parameters
    ----------
    nodules : pd.DataFrame
        contains:
         - 'seriesuid': index of patient or series.
         - 'z','y','x': coordinates of nodules center.
         - 'diameter': diameter, in mm.
    fmt : str
        can be either 'raw', 'blosc' or 'dicom'.
    nodule_shape : tuple, list or ndarray of int
        crop shape along (z,y,x).
    batch_size : int
        number of nodules in batch generated by pipeline.
    share : float
        share of cancer crops in the batch.
    histo : tuple
        :func:`numpy.histogramdd` output.
        Used for sampling non-cancerous crops
    variance : tuple, list or ndarray of float
        variances of normally distributed random shifts of
        nodules' start positions
    hu_lims : tuple, list of float
        seq of len=2, representing limits of hu-trimming in normalize_hu-action.
    **kwargs
            spacing : tuple
                (z,y,x) spacing after resize.
            shape : tuple
                (z,y,x) shape after crop/pad.
            method : str
                interpolation method ('pil-simd' or 'resize').
                See :func:`~radio.CTImagesBatch.resize`.
            order : None or int
                order of scipy-interpolation (<=5), if used.
            padding : str
                mode of padding, any supported by :func:`numpy.pad`.

    Returns
    -------
    pipeline
    """
    # update args of unify spacing
    args_unify_spacing = copy(kwargs_default)
    args_unify_spacing.update(kwargs)

    # set up other args-dicts
    args_sample_nodules = dict(nodule_size=nodule_shape, batch_size=batch_size, share=share,
                               histo=histo, variance=variance)

    # set up the pipeline
    pipeline = (Pipeline()
                .load(fmt=fmt)
                .fetch_nodules_info(nodules=nodules)
                .unify_spacing(**args_unify_spacing)
                .create_mask()
                .normalize_hu(min_hu=hu_lims[0], max_hu=hu_lims[1])
                .sample_nodules(**args_sample_nodules)
                .run(lazy=True, batch_size=RUN_BATCH_SIZE, shuffle=True)
               )

    return pipeline


def split_dump(cancer_path, non_cancer_path, nodules, histo=None, fmt='raw',
               nodule_shape=(32, 64, 64), variance=(36, 144, 144), **kwargs):
    """ Get pipeline for dumping cancerous crops in one folder and random noncancerous crops in another.

    Parameters
    ----------
    cancer_path : str
        directory to dump cancerous crops in.
    non_cancer_path : str
        directory to dump non-cancerous crops in.
    nodules : pd.DataFrame
        contains:
         - 'seriesuid': index of patient or series.
         - 'z','y','x': coordinates of nodules center.
         - 'diameter': diameter, in mm.
    histo : tuple
        :func:`numpy.histogramdd` output.
        Used for sampling non-cancerous crops
    fmt : str
        can be either 'raw', 'blosc' or 'dicom'.
    nodule_shape : tuple, list or ndarray of int
        crop shape along (z,y,x).
    variance : tuple, list or ndarray of float
        variances of normally distributed random shifts of
        nodules' start positions
    **kwargs
            spacing : tuple
                (z,y,x) spacing after resize.
            shape : tuple
                (z,y,x) shape after crop/pad.
            method : str
                interpolation method ('pil-simd' or 'resize').
                See :func:`~radio.CTImagesBatch.resize` for more information.
            order : None or int
                order of scipy-interpolation (<=5), if used.
            padding : str
                mode of padding, any supported by :func:`numpy.pad`.

    Returns
    -------
    pipeline
    """
    # update args of unify spacing
    args_unify_spacing = copy(kwargs_default)
    args_unify_spacing.update(kwargs)

    # set up args-dicts
    args_dump_cancer = dict(dst=cancer_path, n_iters=N_ITERS, nodule_size=nodule_shape,
                            variance=variance, share=1.0, batch_size=None)
    args_sample_ncancer = dict(nodule_size=nodule_shape, histo=histo,
                               batch_size=NON_CANCER_BATCH_SIZE, share=0.0)

    # define pipeline. Two separate tasks are performed at once, in one run:
    # 1) sampling and dumping of cancerous crops in wrapper-action sample_dump
    # 2) sampling and dumping of non-cancerous crops in separate actions
    pipeline = (Pipeline()
                .load(fmt=fmt)
                .fetch_nodules_info(nodules=nodules)
                .unify_spacing(**args_unify_spacing)
                .create_mask()
                .sample_dump(**args_dump_cancer)  # sample and dump cancerous crops
                .sample_nodules(**args_sample_ncancer)  # sample non-cancerous
                .dump(dst=non_cancer_path)  # dump non-cancerous
                .run(lazy=True, batch_size=RUN_BATCH_SIZE, shuffle=False)
               )

    return pipeline

def update_histo(nodules, histo, fmt='raw', **kwargs):
    """ Pipeline for updating histogram using info in dataset of scans.

    Parameters
    ----------
    nodules : pd.DataFrame
        contains:
         - 'seriesuid': index of patient or series.
         - 'z','y','x': coordinates of nodules center.
         - 'diameter': diameter, in mm.
    histo : tuple
        :func:`numpy.histogramdd` output.
        Used for sampling non-cancerous crops
        (compare the latter with tuple (bins, edges) returned by :func:`numpy.histogramdd`).
    fmt : str
        can be either 'raw', 'blosc' or 'dicom'.
    **kwargs
            spacing : tuple
                (z,y,x) spacing after resize.
            shape : tuple
                (z,y,x) shape after crop/pad.
            method : str
                interpolation method ('pil-simd' or 'resize').
                See :func:`~radio.CTImagesBatch.resize` for more information.
            order : None or int
                order of scipy-interpolation (<=5), if used.
            padding : str
                mode of padding, any supported by :func:`numpy.pad`.

    Returns
    -------
    pipeline
    """
    # update args of unify spacing
    args_unify_spacing = copy(kwargs_default)
    args_unify_spacing.update(kwargs)

    # perform unify_spacing and call histo-updating action
    pipeline = (Pipeline()
                .load(fmt=fmt)
                .fetch_nodules_info(nodules=nodules)
                .unify_spacing(**args_unify_spacing)
                .create_mask()
                .update_nodules_histo(histo)
                .run(lazy=True, batch_size=RUN_BATCH_SIZE, shuffle=False)
               )

    return pipeline

def combine_crops(cancer_set, non_cancer_set, batch_sizes=(10, 10), hu_lims=(-1000, 400), shuffle=True):
    """ Pipeline for generating batches of cancerous and non-cancerous crops from
    ct-scans in chosen proportion.

    Parameters
    ---------
    cancer_set : dataset
        dataset of cancerous crops in blosc format.
    non_cancer_set : dataset
        dataset of non-cancerous crops in blosc format.
    batch_sizes : tuple, list of int
        seq of len=2, (num_cancer_batches, num_noncancer_batches).
    hu_lims : tuple, list of float
        seq of len=2, representing limits of hu-trimming in normalize_hu-action.

    Returns
    -------
    pipeline
    """
    # pipeline generating cancerous crops
    ppl_cancer = (cancer_set.p
                  .load(fmt='blosc')
                  .normalize_hu(min_hu=hu_lims[0], max_hu=hu_lims[1])
                  .run(lazy=True, batch_size=batch_sizes[0], shuffle=shuffle, drop_last=True)
                 )

    # pipeline generating non-cancerous crops merged with first pipeline
    pipeline = (non_cancer_set.p
                .load(fmt='blosc')
                .normalize_hu(min_hu=hu_lims[0], max_hu=hu_lims[1])
                .merge(ppl_cancer)
                .run(lazy=True, batch_size=batch_sizes[1], shuffle=shuffle, drop_last=True)
               )

    return pipeline
