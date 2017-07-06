# pylint: disable=no-name-in-module, undefined-variable, anomalous-backslash-in-string
""" contains Batch class for storing Ct-scans """

import os
import pickle

import numpy as np
import aiofiles
import blosc
import dicom
import SimpleITK as sitk

from .dataset_import import Batch, action, inbatch_parallel, any_action_failed

from .resize import resize_patient_numba
from .segment import calc_lung_mask_numba
from .mip import xip_fn_numba
from .flip import flip_patient_numba
from .crop import return_black_border_array as rbba
from .patches import put_patches_numba, assemble_patches, calc_padding_size


AIR_HU = -2000
DARK_HU = -2000


class CTImagesBatch(Batch): # pylint: disable=too-many-public-methods

    """
    class for storing batch of CT(computed tomography) 3d-scans.
        Derived from base class Batch


    Attrs:
        1. index: array of PatientIDs. Usually, PatientIDs are strings
        2. _data: 3d-array of stacked scans along number_of_slices axis
        3. _bounds: 1d-array of bound-floors for each patient
            has length = number of patients + 1


    Important methods:
        1. __init__(self, index):
            basic initialization of patient
            in accordance with Batch.__init__
            given base class Batch

        2. load(self, source, fmt, upper_bounds):

            builds skyscraper of patients
            from either 'dicom'|'raw'|'blosc'|'ndarray'
            returns self

        2. resize(self, shape, order):
            transform the shape of all patients to new_sizes
            method is spline iterpolation(order = order)
            the function is multithreaded
            returns self

        3. dump(self, dst)
            create a dump of the batch
            in the path-folder
            returns self

        4. calc_lungs_mask(self, erosion_radius=7)
            returns binary-mask for lungs segmentation
            the larger erosion_radius
            the lesser the resulting lungs will be
            * returns mask, not self

        5. segment(self, erosion_radius=2)
            segments using mask from calc_lungs_mask()
            that is, sets to hu = -2000 of pixels outside mask
            changes self, returns self

        6. flip(self)
            invert slices corresponding to each patient
            do not change the order of patients
            changes self, returns self

        7. normalize_hu(self, min_hu=-1000, max_hu=400):
            normalizes hu-densities to interval [0, 255]
            trims hus outside range [min_hu, max_hu]
            then scales to [0, 255]
            changes self, returns self

    """

    def __init__(self, index):
        """
        common part of initialization from all formats:
            -execution of Batch construction
            -initialization of all attrs
            -creation of empty lists and arrays

        attrs:
            index - ndarray of indices
            dtype is likely to be string
        """

        super().__init__(index)

        self._data = None
        self._bounds = None
        self.origin = None
        self.spacing = None

        self._init_data()

        self._crop_centers = np.array([], dtype=np.int32)
        self._crop_sizes = np.array([], dtype=np.int32)

    def _init_data(self, source=None, bounds=None, origin=None, spacing=None):
        """Initialize _data, _bounds, _crop_centers, _crop_sizes atteributes.

        This method is called inside __init__ and some other methods
        and used as initializer of batch inner structures.

        Args:
        - source: ndarray(n_patients * k, l, m) or None(default) that will be used
        as self._data;
        - bounds: ndarray(n_patients, dtype=np.int) or None(default) that will be
        uses as self._bounds;
        - origin: ndarray(n_patients, 3) or None(default) that will be used as
        self.origin attribute representing patients origins in world coordinate
        system. None value will be converted to zero-array.
        - spacing: ndarray(n_patients, 3) or None(default) that will be used as
        self.spacing attribute representing patients spacings in world coordinate
        system. None value will be converted to ones-array.
        """
        # pylint: disable=attribute-defined-outside-init
        self._data = source
        self._bounds = bounds if bounds is not None else np.array([], dtype='int')
        self.origin = origin if origin is not None else np.zeros((len(self), 3))
        self.spacing = spacing if spacing is not None else np.ones((len(self), 3))

    @action
    def load(self, fmt='dicom', source=None, bounds=None,
             origin=None, spacing=None, src_blosc=('data', 'attrs')):    # pylint: disable=arguments-differ
        """ Loads 3d scans-data in batch

        Args:
            fmt: type of data. Can be 'dicom'|'blosc'|'raw'|'ndarray'
            source: source array with skyscraper, needed iff fmt = 'ndarray'
            bounds: bound floors for patients. Needed iff fmt='ndarray'
            origin: ndarray [len(bounds) X 3] with world coords of each patient's
                starting pixels. Needed only if fmt='ndarray'
            spacing: ndarray [len(bounds) X 3] with spacings of patients.
                Needed only if fmt='ndarray'
            src_blosc: iterable with components of batch that should be loaded from blosc

        Return:
            self

        Dicom example:

            # initialize batch for storing batch of 3 patients
            # with following IDs
            index = FilesIndex(path="/some/path/*.dcm", no_ext=True)
            batch = CTImagesBatch(index)
            batch.load(fmt='dicom')

        Ndarray example:
            # source_array stores a batch (concatted 3d-scans, skyscraper)
            # say, ndarray with shape (400, 256, 256)

            # bounds stores ndarray of last floors for each patient
            # say, source_ubounds = np.asarray([0, 100, 400])
            batch.load(source=source_array, fmt='ndarray', bounds=bounds)

        """
        # if ndarray
        if fmt == 'ndarray':
            self._init_data(source, bounds, origin, spacing)
        elif fmt == 'dicom':
            self._load_dicom()              # pylint: disable=no-value-for-parameter
        elif fmt == 'blosc':
            self._load_blosc(src=src_blosc)              # pylint: disable=no-value-for-parameter
        elif fmt == 'raw':
            self._load_raw()                # pylint: disable=no-value-for-parameter
        else:
            raise TypeError("Incorrect type of batch source")
        return self


    @inbatch_parallel(init='indices', post='_post_default', target='threads')
    def _load_dicom(self, patient_id, *args, **kwargs):                # pylint: disable=unused-argument
        """
        read, prepare and put stacked 3d-scans in an array
            return the array

        args:
            patient_id - index of patient from batch, whose scans we need to
            stack

        Important operations performed here:
         - conversion to hu using meta from dicom-scans
        """
        patient_folder = self.index.get_fullpath(patient_id)

        list_of_dicoms = [dicom.read_file(os.path.join(patient_folder, s)) for s in os.listdir(patient_folder)]

        list_of_dicoms.sort(key=lambda x: int(x.ImagePositionPatient[2]), reverse=True)
        intercept_pat = list_of_dicoms[0].RescaleIntercept
        slope_pat = list_of_dicoms[0].RescaleSlope

        patient_data = np.stack([s.pixel_array for s in list_of_dicoms]).astype(np.int16)

        patient_data[patient_data == AIR_HU] = 0

        if slope_pat != 1:
            patient_data = slope_pat * patient_data.astype(np.float64)
            patient_data = patient_data.astype(np.int16)

        patient_data += np.int16(intercept_pat)
        return patient_data

    @inbatch_parallel(init='indices', post='_post_default', target='async', update=False)
    async def _load_blosc(self, patient_id, *args, **kwargs):                # pylint: disable=unused-argument
        """
        Read, prepare scans from blosc and put them into the right place in the
            skyscraper

        Args:
            patient_id: index of patient from batch, whose scans we need to
                stack
            src: components of data that should be loaded into self


            *no conversion to hu here
        """
        # convert src to iterable 1d-array
        src = np.asarray(kwargs['src']).reshape(-1)

        for source in src:
            # set correct extension for each component and choose a tool
            # for debyting it
            if source in ['attrs', 'spacing', 'origin']:
                ext = '.pkl'
                unpacker = pickle.loads
            else:
                ext = '.blk'
                unpacker = blosc.unpack_array

            comp_path = os.path.join(self.index.get_fullpath(patient_id), source + ext)

            # read the component
            async with aiofiles.open(comp_path, mode='rb') as file:
                byted = await file.read()

            # de-byte it with the chosen tool
            component = unpacker(byted)

            # put the component in batch
            getattr(self, source)[patient_id] = component

        return None

    def _load_raw(self, **kwargs):        # pylint: disable=unused-argument
        """
        read, prepare and put 3d-scans in list

            *no conversion to hu here
        """
        list_of_arrs = []
        for patient_id in self.indices:
            raw_data = sitk.ReadImage(self.index.get_fullpath(patient_id))
            patient_pos = self.index.get_pos(patient_id)
            list_of_arrs.append(sitk.GetArrayFromImage(raw_data))
            # *.mhd files contain information about scans' origin and spacing;
            # however the order of axes there is inversed:
            # so, we just need to reverse arrays with spacing and origin.
            self.origin[patient_pos, :] = np.array(raw_data.GetOrigin())[::-1]
            self.spacing[patient_pos, :] = np.array(raw_data.GetSpacing())[::-1]

        new_data = np.concatenate(list_of_arrs, axis=0)
        new_bounds = np.cumsum(np.array([len(a) for a in [[]] + list_of_arrs]))
        self._data = new_data
        self._bounds = new_bounds
        return self

    @staticmethod
    async def dump_data(data_items, folder):
        """ Dump data that contains in data_items on disk in
                specified folder

        Args:
            data_items: dict of data items for dump in form item_name.ext: item
                (e.g.: {'data.blk': data, 'mask.blk': mask, 'attrs.pkl': attrs})
            folder: folder to dump data-items in

        Return:
            ____

        *NOTE: depending on supplied format, each data-item will be either
            serialized (if .pkl) or blosc-packed (if .blk)
        """

        # create directory if does not exist
        if not os.path.exists(folder):
            os.makedirs(folder)

        # infer extension of each item, serialize/blosc-pack and dump the item
        for filename, data in data_items.items():
            ext = filename.split('.')[-1]
            if ext == 'blk':
                byted = blosc.pack_array(data, cname='zstd', clevel=1)
            elif ext == 'pkl':
                byted = pickle.dumps(data)
            async with aiofiles.open(os.path.join(folder, filename),
                                     mode='wb') as file:
                _ = await file.write(byted)

        return None

    @action
    @inbatch_parallel(init='indices', post='_post_default', target='async', update=False)
    async def dump(self, patient, dst, src='data', fmt='blosc'):
        """ Dump scans data (3d-array) on specified path in specified format

        Args:
            dst: general folder in which all patients' data should be put
            fmt: format of dump. Currently only blosc-format is supported;
                in this case folder for each patient is created, patient's data
                is put into data.blk, attributes are put into dict attrs.pkl


        example:
            # initialize batch and load data
            ind = ['1ae34g90', '3hf82s76', '2ds38d04']
            batch = CTImagesBatch(ind)
            batch.load(...)
            batch.dump(dst='./data/blosc_preprocessed')
            # the command above creates files

            # ./data/blosc_preprocessed/1ae34g90/data.blk
            # ./data/blosc_preprocessed/1ae34g90/attrs.pkl

            # ./data/blosc_preprocessed/3hf82s76/data.blk
            # ./data/blosc_preprocessed/1ae34g90/attrs.pkl

            # ./data/blosc_preprocessed/2ds38d04/data.blk
            # ./data/blosc_preprocessed/1ae34g90/attrs.pkl
        """
        if fmt != 'blosc':
            raise NotImplementedError('Dump to {} is not implemented yet'.format(fmt))

        # convert src to iterable 1d-array
        src = np.asarray(src).reshape(-1)
        data_items = dict()

        # set correct extension to each component and add it to items-dict
        for source in list(src):
            if source in ['attrs', 'spacing', 'origin']:
                ext = '.pkl'
            else:
                ext = '.blk'
            data_items.update({source + ext: getattr(self, source)[patient]})

        # set patient-specific folder
        folder = os.path.join(dst, patient)

        return await self.dump_data(data_items, folder)

    @property
    def components(self):
        """ Names for components of tuple returned from __getitem__
        """
        return 'data', 'attrs', 'spacing', 'origin'

    def __len__(self):
        """ Get number of patients in self

        Return:
            number of patients in batch

        """
        return len(self.index)

    def __getitem__(self, index):
        """ Indexation of patients by []

        Args:
            self
            index - can be either number (int) of patient
                         in self from [0,..,len(self.index) - 1]
                    or index from self.index
        Return:
            components of patient's data with index/number given by index;
            components are (scan, attrs, spacing, origin)
        """
        return (self.get_image(index), self.get_attrs(index),
                self.spacing[self._get_verified_pos(index)],
                self.origin[self._get_verified_pos(index)])

    def _get_verified_pos(self, index):
        """Get verified position of patient in batch by index.       # pylint: disable=anomalous-backslash-in-string

        Firstly, check if index is instance of str or int. If int
        then it is supposed that index represents patient's position in Batch.
        If fetched position is out of bounds then Exception is generated.
        If str then position of patient is fetched.

        Args:
            index - can be either position of patient in self._data
                or index from self.index

        Return:
            if supplied index is int, return supplied number,
            o\w return the position of patient with supplied index
        """
        if isinstance(index, int):
            if index < len(self) and index >= 0:
                pos = index
            else:
                raise IndexError("Index is out of range")
        else:
            pos = self.index.get_pos(index)
        return pos

    def get_image(self, index):
        """ Get view on patient's data

        Args:
            index: either position of patient in self._data
                or index from self.index

        Return:
            ndarray(view) with the data of chosen patient
        """
        pos = self._get_verified_pos(index)
        return self._data[self.lower_bounds[pos]: self.upper_bounds[pos], :, :]


    def get_attrs(self, index):
        """ Get attributes of a patient

        Args:
            index: either position of patient in self._data
                or index from self.index

        Return:
            dict with attributes of chosen patient
        """
        pos = self._get_verified_pos(index)
        origin = self.origin[pos, :]
        spacing = self.spacing[pos, :]
        attrs = {'spacing': spacing, 'origin': origin}
        return attrs

    @property
    def shape(self):
        """Get CTImages shapes for all patients in CTImagesBatch.

        This property returns ndarray(n_patients, 3) containing
        shapes of data for each patient(first dimension).
        """
        shapes = np.zeros((len(self), 3), dtype=np.int)
        shapes[:, 0] = self.upper_bounds - self.lower_bounds
        shapes[:, 1], shapes[:, 2] = self.slice_shape
        return shapes

    @property
    def lower_bounds(self):
        """Get lower bounds of patients data in CTImagesBatch.

        This property returns ndarray(n_patients,) containing
        lower bounds of patients data along z-axis.
        """
        return self._bounds[:-1]

    @property
    def upper_bounds(self):
        """Get upper bounds of patients data in CTImagesBatch.

        This property returns ndarray(n_patients,) containing
        upper bounds of patients data along z-axis.
        """
        return self._bounds[1:]

    @property
    def slice_shape(self):
        """Get shape of slice in yx-plane.

        This property returns ndarray(2,) containing shape of scan slice
        in yx-plane.
        """
        return np.asarray(self._data.shape[1:])

    def rescale(self, new_shape):
        """Rescale patients' spacing parameter after resise.

        This method recomputes spacing values
        for patients' data stored in CTImagesBatch after resize operation.

        Args:
        - new_shape: new shape of single patient data array, supposed to be
        np.array([j, k, l], dtype=np.int) where j, k, l -- sizes of
        single patient data array along z, y, x correspondingly;
        Return:
        - new_spacing: ndarray(n_patients, 3) with spacing values for each
        patient along z, y, x axes.
        """
        return (self.spacing * self.shape) / new_shape

    def _post_default(self, list_of_arrs, update=True, new_batch=False, **kwargs):    # pylint: disable=unused-argument
        """
        gatherer of outputs of different workers
            assumes that output of each worker corresponds to patient data
        """
        if any_action_failed(list_of_arrs):
            raise ValueError("Failed while parallelizing")

        res = self
        if update:
            new_data = np.concatenate(list_of_arrs, axis=0)
            new_bounds = np.cumsum(np.array([len(a) for a in [[]] + list_of_arrs]))
            params = dict(source=new_data, bounds=new_bounds,
                          origin=self.origin, spacing=self.spacing)
            if new_batch:
                batch = type(self)(self.index)
                batch.load(fmt='ndarray', **params)
                res = batch
            else:
                self._init_data(**params)
        return res

    def _init_images(self, **kwargs):               # pylint: disable=unused-argument
        return [self.get_image(patient_id) for patient_id in self.indices]

    def _post_crop(self, list_of_arrs, **kwargs):   # pylint: disable=unused-argument
        # TODO: check for errors
        crop_array = np.array(list_of_arrs)
        self._crop_centers = crop_array[:, :, 2]
        self._crop_sizes = crop_array[:, :, : 2]

    def _init_rebuild(self, **kwargs):
        """
        args-fetcher for parallelization using decorator
            can be used when batch-data is rebuild from scratch
        if shape is supplied as one of the args, assumes that data
            should be resized, resize is performed
        if spacing is supplied as one of the args, assumes that unify_spacing
            is performed
        """
        if 'shape' in kwargs:
            num_slices, y, x = kwargs['shape']
            new_bounds = num_slices * np.arange(len(self) + 1)
            new_data = np.zeros((num_slices * len(self), y, x))
        else:
            new_bounds = self._bounds
            new_data = np.zeros_like(self._data)

        all_args = []
        for i in range(len(self)):
            out_patient = new_data[new_bounds[i]: new_bounds[i + 1], :, :]
            item_args = {'patient': self.get_image(i),
                         'out_patient': out_patient,
                         'res': new_data}
            # for unify_spacing
            if 'spacing' in kwargs:
                item_args['res_factor'] = self.spacing[i, :] / \
                                          np.array(kwargs['spacing'])

            all_args += [item_args]

        return all_args

    def _post_rebuild(self, all_outputs, new_batch=False, **kwargs):   # pylint: disable=unused-argument
        """
        gatherer of outputs from different workers for
            ops, requiring complete rebuild of batch._data
        args:
            new_batch: if True, returns new batch with data
                agregated from workers_ouputs
        """
        if any_action_failed(all_outputs):
            raise ValueError("Failed while parallelizing")

        new_bounds = np.cumsum([patient_shape[0] for _, patient_shape
                                in [[0, (0, )]] + all_outputs])
        # each worker returns the same ref to the whole res array
        new_data, _ = all_outputs[0]

        # recalculate new_attrs of a batch

        # for resize/unify_spacing: if shape is supplied, assume post
        # is for resize or unify_spacing
        if 'shape' in kwargs:
            new_spacing = self.rescale(kwargs['shape'])
        else:
            new_spacing = self.spacing

        # for unify_spacing: if spacing is supplied, assume post
        # is for unify_spacing
        if 'spacing' in kwargs:
            # recalculate origin, spacing
            shape_after_resize = np.rint(self.shape * self.spacing /
                                         np.asarray(kwargs['spacing']))
            overshoot = shape_after_resize - np.asarray(kwargs['shape'])
            new_spacing = self.rescale(new_shape=shape_after_resize)
            new_origin = self.origin + new_spacing * (overshoot // 2)
        else:
            new_origin = self.origin

        # build/update batch with new data and attrs
        params = dict(source=new_data, bounds=new_bounds,
                      origin=new_origin, spacing=new_spacing)
        if new_batch:
            batch_res = type(self)(self.index)
            batch_res.load(fmt='ndarray', **params)
            return batch_res
        else:
            self._init_data(**params)
            return self

    @property
    def crop_centers(self):
        """
        returns centers of crop for all scans
        """
        if not self._crop_centers:
            self._crop_params_patients()
        return self._crop_centers

    @property
    def crop_sizes(self):
        """
        returns window sizes for crops
        """
        if not self._crop_sizes:
            self._crop_params_patients()
        return self._crop_sizes


    @inbatch_parallel(init='_init_images', post='_post_crop', target='nogil')
    def _crop_params_patients(self, *args, **kwargs):                    # pylint: disable=unused-argument,no-self-use
        """
        calculate params for crop, calling return_black_border_array
        """
        return rbba

    @action
    @inbatch_parallel(init='_init_rebuild', post='_post_rebuild', target='nogil')
    def resize(self, shape=(128, 256, 256), order=3, *args, **kwargs):    # pylint: disable=unused-argument, no-self-use
        """ Resize (change shape) each CT-scan in the batch.
                When called from a batch, changes this batch.
        Args:
            shape: needed shape after resize in order z, y, x
                *note that the order of axes in data is z, y, x
                 that is, new patient shape = (shape[0], shape[1], shape[2])
            n_workers: number of threads used (degree of parallelism)
                *note: available in the result of decoration of the function
                above
            order: the order of interpolation (<= 5)
                large value improves precision, but slows down the computaion
        Returns:
            self
        example:
            shape = (128, 256, 256)
            Batch = Batch.resize(shape=shape, n_workers=20, order=2)
        """
        return resize_patient_numba

    @action
    @inbatch_parallel(init='_init_rebuild', post='_post_rebuild', target='nogil')
    def unify_spacing(self, spacing=(1, 1, 1), shape=(128, 256, 256), order=3,    # pylint: disable=unused-argument, no-self-use
                      padding='edge'):                                            # pylint: disable=unused-argument
        """ Unify spacing of all patients using resize, then crop/pad resized array
                to supplied shape.
        Args:
            spacing: needed spacing in mm
            shape: needed shape after crop/pad
            order: order of interpolation (<=5)
            padding: mode of padding, any of those supported by np.pad
        Return:
            self
        """
        return resize_patient_numba



    @action
    @inbatch_parallel(init='_init_images', post='_post_default', target='nogil', new_batch=True)
    def make_xip(self, step=2, depth=10, func='max', projection='axial', *args, **kwargs):    # pylint: disable=unused-argument, no-self-use
        """
        This function takes 3d picture represented by np.ndarray image,
        start position for 0-axis index, stop position for 0-axis index,
        step parameter which represents the step across 0-axis and, finally,
        depth parameter which is associated with the depth of slices across
        0-axis made on each step for computing MEAN, MAX, MIN
        depending on func argument.
        Possible values for func are 'max', 'min' and 'avg'.
        Notice that 0-axis in this annotation is defined in accordance with
        projection argument which may take the following values: 'axial',
        'coroanal', 'sagital'.
        Suppose that input 3d-picture has axis associations [z, x, y], then
        axial projection doesn't change the order of axis and 0-axis will
        be correspond to 0-axis of the input array.
        However in case of 'coronal' and 'sagital' projections the source tensor
        axises will be transposed as [x, z, y] and [y, z, x]
        for 'coronal' and 'sagital' projections correspondingly.
        """
        return xip_fn_numba(func, projection, step, depth)

    @inbatch_parallel(init='_init_rebuild', post='_post_rebuild', target='nogil', new_batch=True)
    def calc_lung_mask(self, *args, **kwargs):     # pylint: disable=unused-argument, no-self-use
        """ Return a mask for lungs """
        return calc_lung_mask_numba

    @action
    def segment(self, erosion_radius=2):
        """
        lungs segmenting function
            changes self

        sets hu of every pixes outside lungs
            to DARK_HU

        example:
            batch = batch.segment(erosion_radius=4, num_threads=20)
        """
        # get mask with specified params
        # reverse it and set not-lungs to DARK_HU

        mask_batch = self.calc_lung_mask(erosion_radius=erosion_radius)
        lungs_mask = mask_batch.data
        self._data = self._data * lungs_mask

        result_mask = 1 - lungs_mask
        result_mask *= DARK_HU

        self._data += result_mask

        return self


    def get_patches(self, patch_shape, stride, padding='edge', data_attr='_data'):
        """
        extract patches of size patch_shape with specified
            stride
        Args:
            patch_shape: tuple/list/ndarray of len=3 with needed
                patch shape
            stride: tuple/list/ndarray of len=3 with stride that we
                use to slide over each patient's data
            padding: type of padding (see doc of np.pad for available types)
                say, 3.6 windows of size=patch_shape with stride
                can be exracted from each patient's data.
                Then data will be padded s.t. 4 windows can be extracted
            data_attr: name of attribute where the data is stored
                _data by default
        Return:
            4d-ndaray of patches; first dimension enumerates patches
        *Note: the shape of all patients is assumed to be the same
        """

        patch_shape, stride = np.asarray(patch_shape), np.asarray(stride)
        img_shape = self.shape[0]
        data_4d = np.reshape(getattr(self, data_attr), (-1, ) + tuple(img_shape))

        # add padding if necessary
        pad_width = calc_padding_size(img_shape, patch_shape, stride)
        if pad_width is not None:
            data_padded = np.pad(data_4d, pad_width, mode=padding)
        else:
            data_padded = data_4d

        # init tensor with patches
        num_sections = (np.asarray(data_padded.shape[1 : ]) - patch_shape) // stride + 1
        patches = np.zeros(shape=(len(self),
                                  np.prod(num_sections)) + tuple(patch_shape))

        # put patches into the tensor
        fake = np.zeros(len(self))
        put_patches_numba(data_padded, patch_shape, stride, patches, fake)
        patches = np.reshape(patches, (len(self) * \
            np.prod(num_sections), ) + tuple(patch_shape))
        return patches

    def load_from_patches(self, patches, stride, scan_shape, data_attr='_data'):
        """
        Assemble skyscraper from 4d-array of patches and
            put it into data_attr-attribute of batch
        Args:
            patches: 4d-array of patches, first dim enumerates patches
                others are spatial in order (z, y, x)
            scan_shape: tuple/ndarray/list of len=3 with shape of scan-array
                patches are assembled into; order of axis is (z, y, x)
            stride: tuple/ndarray/list of len=3 with stride with which
                patches are put into data_attr;
                if stride != patch shape, averaging over overlapping regions
                    is used
            data_attr: the name of attribute, the assembled skyscraper should
                be put into
            *note: scan_shape, patches.shape, sride are used to infer the number
                of sections;
                in case of overshoot we crop the padding out
        """
        scan_shape, stride = np.asarray(scan_shape), np.asarray(stride)
        patch_shape = np.asarray(patches.shape[1 : ])

        # infer what padding was applied to scans when extracting patches
        pad_width = calc_padding_size(scan_shape, patch_shape, stride)

        # if padding is non-zero, adjust the shape of scan
        if pad_width is not None:
            shape_delta = np.asarray(
                [before + after for before, after in pad_width[1 : ]])
        else:
            shape_delta = np.zeros(3).astype('int')

        scan_shape_adj = scan_shape + shape_delta

        # init 4d tensor and put assembled scans into it
        data_4d = np.zeros((len(self), ) + tuple(scan_shape_adj))
        patches = np.reshape(patches, (len(self), -1) + tuple(patch_shape))
        fake = np.zeros(len(self))
        assemble_patches(patches, stride, data_4d, fake)

        # crop (perform anti-padding) if necessary
        if pad_width is not None:
            data_shape = data_4d.shape
            slc_z = slice(pad_width[1][0], data_shape[1] - pad_width[1][1])
            slc_y = slice(pad_width[2][0], data_shape[2] - pad_width[2][1])
            slc_x = slice(pad_width[3][0], data_shape[3] - pad_width[3][1])
            data_4d = data_4d[:, slc_z, slc_y, slc_x]

        # reshape 4d-data to skyscraper form and put it into needed attr
        data_4d = data_4d.reshape((len(self) * scan_shape[0], ) + \
            tuple(scan_shape[1 : ]))
        setattr(self, data_attr, data_4d)



    @action
    def normalize_hu(self, min_hu=-1000, max_hu=400):
        """
        normalizes hu-densities to interval [0, 255]:
            trims hus outside range [min_hu, max_hu]
            then scales to [0, 255]

        example:
            batch = batch.normalize_hu(min_hu=-1300, max_hu=600)
        """

        # trimming and scaling to [0, 1]
        self._data = (self._data - min_hu) / (max_hu - min_hu)
        self._data[self._data > 1] = 1.
        self._data[self._data < 0] = 0.

        # scaling to [0, 255]
        self._data *= 255
        return self

    @action
    @inbatch_parallel(init='_init_rebuild', post='_post_rebuild', target='nogil')
    def flip(self):    # pylint: disable=no-self-use
        """
        flip each patient
            i.e. invert the order of slices for each patient
            does not change the order of patients
            changes self

        example:
            batch = batch.flip()
        """
        return flip_patient_numba

    def get_axial_slice(self, person_number, slice_height):
        """
        get axial slice (e.g., for plots)

        args: person_number - can be either
            number of person in the batch
            or index of the person
                whose axial slice we need

        slice_height: e.g. 0.7 means that we take slice with number
            int(0.7 * number of slices for person)

        example: patch = batch.get_axial_slice(5, 0.6)
                 patch = batch.get_axial_slice(self.index[5], 0.6)
                 # here self.index[5] usually smth like 'a1de03fz29kf6h2'

        """
        margin = int(slice_height * self[person_number].shape[0])
        patch = self[person_number][margin, :, :]
        return patch
