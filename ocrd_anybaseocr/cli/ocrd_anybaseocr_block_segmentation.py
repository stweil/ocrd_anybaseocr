# pylint: disable=missing-module-docstring, missing-class-docstring, invalid-name
# pylint: disable=line-too-long, import-error, no-name-in-module, too-many-statements
# pylint: disable=wrong-import-position, wrong-import-order, too-many-locals, too-few-public-methods
import sys
import os
from pathlib import Path
from pkg_resources import resource_filename

import click
import cv2
import numpy as np
from shapely.geometry import Polygon
import ocrolib


from ocrd import Processor
from ocrd.decorators import ocrd_cli_options, ocrd_cli_wrap_processor
from ocrd_modelfactory import page_from_file
from ocrd_utils import (
    getLogger,
    make_file_id,
    assert_file_grp_cardinality,
    MIMETYPE_PAGE,
    coordinates_for_segment,
    points_from_polygon,
    polygon_from_points
)
from ocrd_models.ocrd_page import (
    CoordsType,
    TextRegionType,
    GraphicRegionType,
    TableRegionType,
    ImageRegionType,
    to_xml,
    RegionRefIndexedType, OrderedGroupType, ReadingOrderType
)
from ..mrcnn import model
from ..mrcnn.config import Config
from ..constants import OCRD_TOOL
from ..tensorflow_importer import tf


TOOL = 'ocrd-anybaseocr-block-segmentation'
FALLBACK_IMAGE_GRP = 'OCR-D-IMG-BLOCK-SEGMENT'

CLASS_NAMES = ['BG',
               'page-number',
               'paragraph',
               'catch-word',
               'heading',
               'drop-capital',
               'signature-mark',
               'header',
               'marginalia',
               'footnote',
               'footnote-continued',
               'caption',
               'endnote',
               'footer',
               'keynote',
               # not included in the provided models yet:
               #'image',
               #'table',
               #'graphics'
]

class InferenceConfig(Config):

    def __init__(self, confidence):
        Config.__init__(self, confidence)

    NAME = "block"
    IMAGES_PER_GPU = 1
    NUM_CLASSES = len(CLASS_NAMES)

#     NUM_CLASSES = 1 + 14
#     DETECTION_MIN_CONFIDENCE = 0.9 # needs to be changed back to parameter

class OcrdAnybaseocrBlockSegmenter(Processor):

    def __init__(self, *args, **kwargs):
        kwargs['ocrd_tool'] = OCRD_TOOL['tools'][TOOL]
        kwargs['version'] = OCRD_TOOL['version']
        super(OcrdAnybaseocrBlockSegmenter, self).__init__(*args, **kwargs)
        if hasattr(self, 'output_file_grp') and hasattr(self, 'parameter'):
            # processing context
            self.setup()

    def setup(self):
        LOG = getLogger('OcrdAnybaseocrBlockSegmenter')
        #self.reading_order = []
        self.order = 0
        model_path = resource_filename(__name__, '../mrcnn')
        model_weights = Path(self.resolve_resource(self.parameter['block_segmentation_weights']))

        confidence = self.parameter['min_confidence']
        config = InferenceConfig(confidence)
        # TODO: allow selecting active class IDs
        self.mrcnn_model = model.MaskRCNN(mode="inference", model_dir=str(model_path), config=config)
        self.mrcnn_model.load_weights(str(model_weights), by_name=True)
    
    def process(self):

        assert_file_grp_cardinality(self.input_file_grp, 1)
        assert_file_grp_cardinality(self.output_file_grp, 1)

        LOG = getLogger('OcrdAnybaseocrBlockSegmenter')
        if not tf.test.is_gpu_available():
            LOG.warning("Tensorflow cannot detect CUDA installation. Running without GPU will be slow.")

        for input_file in self.input_files:
            pcgts = page_from_file(self.workspace.download_file(input_file))
            self.add_metadata(pcgts)
            page = pcgts.get_Page()
            page_id = input_file.pageId or input_file.ID

            page_image, page_xywh, page_image_info = self.workspace.image_from_page(page, page_id, feature_filter='binarized,deskewed,cropped,clipped,non_text')
            # try to load pixel masks
            try:
                mask_image, mask_xywh, mask_image_info = self.workspace.image_from_page(page, page_id, feature_selector='clipped', feature_filter='binarized,deskewed,cropped,non_text')
            except:
                mask_image = None

            self._process_segment(page_image, page, page_xywh, page_id, input_file, mask_image)

            file_id = make_file_id(input_file, self.output_file_grp)
            pcgts.set_pcGtsId(file_id)
            self.workspace.add_file(
                ID=file_id,
                file_grp=self.output_file_grp,
                pageId=input_file.pageId,
                mimetype=MIMETYPE_PAGE,
                local_filename=os.path.join(self.output_file_grp, file_id + '.xml'),
                content=to_xml(pcgts).encode('utf-8')
            )

    def _process_segment(self, page_image, page, page_xywh, page_id, input_file, mask):
        LOG = getLogger('OcrdAnybaseocrBlockSegmenter')
        # check for existing text regions and whether to overwrite them
        if page.get_TextRegion() or page.get_TableRegion():
            if self.parameter['overwrite']:
                LOG.info('removing existing text/table regions in page "%s"', page_id)
                page.set_TextRegion([])
            else:
                LOG.warning('keeping existing text/table regions in page "%s"', page_id)
        # check if border exists
        border = None
        if page.get_Border():
            border_coords = page.get_Border().get_Coords()
            border_points = polygon_from_points(border_coords.get_points())
            border = Polygon(border_points)

        LOG.info('detecting regions on page "%s"', page_id)
        img_array = ocrolib.pil2array(page_image)
        if len(img_array.shape) <= 2:
            img_array = np.stack((img_array,)*3, axis=-1)
        results = self.mrcnn_model.detect([img_array], verbose=0)
        r = results[0]
        LOG.info('found %d regions on page "%s"', len(r['rois']), page_id)

        th = self.parameter['th']
        # check for existing semgentation mask
        # this code executes only when the workflow had tiseg run before with use_deeplr=true
        if mask:
            mask = ocrolib.pil2array(mask)
            mask = mask//255
            mask = 1-mask
            # multiply all the bounding box part with 2
            for i in range(len(r['rois'])):

                min_x = r['rois'][i][0]
                min_y = r['rois'][i][1]
                max_x = r['rois'][i][2]
                max_y = r['rois'][i][3]
                mask[min_x:max_x, min_y:max_y] *= i+2

            # check for left over pixels and add them to the bounding boxes
            pixel_added = True

            while pixel_added:

                pixel_added = False
                left_over = np.where(mask == 1)
                for x, y in zip(left_over[0], left_over[1]):
                    local_mask = mask[x-th:x+th, y-th:y+th]
                    candidates = np.where(local_mask > 1)
                    candidates = [k for k in zip(candidates[0], candidates[1])]
                    if len(candidates) > 0:
                        pixel_added = True
                        # find closest pixel with x>1
                        candidates.sort(key=lambda j: np.sqrt((j[0]-th)**2+(j[1]-th)**2))
                        index = local_mask[candidates[0]]-2

                        # add pixel to mask/bbox
                        # x,y to bbox with index
                        if x < r['rois'][index][0]:
                            r['rois'][index][0] = x

                        elif x > r['rois'][index][2]:
                            r['rois'][index][2] = x

                        if y < r['rois'][index][1]:
                            r['rois'][index][1] = y

                        elif y > r['rois'][index][3]:
                            r['rois'][index][3] = y

                        # update the mask
                        mask[x, y] = index + 2

        # resolving overlapping problem
        bbox_dict = {}  # to check any overlapping bbox
        class_id_check = []

        for i in range(len(r['rois'])):
            min_x = r['rois'][i][0]
            min_y = r['rois'][i][1]
            max_x = r['rois'][i][2]
            max_y = r['rois'][i][3]

            region_bbox = [min_y, min_x, max_y, max_x]

            for key in bbox_dict:
                for bbox in bbox_dict[key]:

                    # checking for ymax case with vertical overlapping
                    # along with y, check both for xmax and xmin
                    if (region_bbox[3] <= bbox[3] and region_bbox[3] >= bbox[1] and
                        ((region_bbox[0] >= bbox[0] and region_bbox[0] <= bbox[2]) or
                         (region_bbox[2] >= bbox[0] and region_bbox[2] <= bbox[2]) or
                         (region_bbox[0] <= bbox[0] and region_bbox[2] >= bbox[2])) and
                        r['class_ids'][i] != 5):

                        r['rois'][i][2] = bbox[1] - 1

                    # checking for ymin now
                    # along with y, check both for xmax and xmin
                    if (region_bbox[1] <= bbox[3] and region_bbox[1] >= bbox[1] and
                        ((region_bbox[0] >= bbox[0] and region_bbox[0] <= bbox[2]) or
                         (region_bbox[2] >= bbox[0] and region_bbox[2] <= bbox[2]) or
                         (region_bbox[0] <= bbox[0] and region_bbox[2] >= bbox[2])) and
                        r['class_ids'][i] != 5):

                        r['rois'][i][0] = bbox[3] + 1

            if r['class_ids'][i] not in class_id_check:
                bbox_dict[r['class_ids'][i]] = []
                class_id_check.append(r['class_ids'][i])

            bbox_dict[r['class_ids'][i]].append(region_bbox)

        # resolving overlapping problem code

        # define reading order on basis of coordinates
        reading_order = []
        for i in range(len(r['rois'])):
            width, height, _ = img_array.shape
            min_x, min_y, max_x, max_y = r['rois'][i]
            class_id = r['class_ids'][i]
            if class_id >= len(CLASS_NAMES):
                raise Exception('Unexpected class id %d - model does not match' % class_id)
            class_name = CLASS_NAMES[class_id]

            if (min_y - 5) > width and class_name == 'paragraph':
                min_y -= 5
            if (max_y + 10) < width and class_name == 'paragraph':
                min_y += 10
            reading_order.append((min_y, min_x, max_y, max_x))

        reading_order = sorted(reading_order, key=lambda reading_order: (reading_order[1], reading_order[0]))
        for i in range(len(reading_order)):
            min_y, min_x, max_y, max_x = reading_order[i]
            min_y = 0
            i_poly = Polygon([[min_x, min_y], [max_x, min_y], [max_x, max_y], [min_x, max_y]])
            for j in range(i+1, len(reading_order)):
                min_y, min_x, max_y, max_x = reading_order[j]
                j_poly = Polygon([[min_x, min_y], [max_x, min_y], [max_x, max_y], [min_x, max_y]])
                inter = i_poly.intersection(j_poly)
                if inter:
                    reading_order.insert(j+1, reading_order[i])
                    del reading_order[i]

        # Creating Reading Order object in PageXML
        order_group = OrderedGroupType(caption="Regions reading order", id=page_id)
        reading_order_object = ReadingOrderType()
        reading_order_object.set_OrderedGroup(order_group)
        page.set_ReadingOrder(reading_order_object)

        for i in range(len(r['rois'])):
            width, height, _ = img_array.shape
            min_x, min_y, max_x, max_y = r['rois'][i]
            class_id = r['class_ids'][i]
            if class_id >= len(CLASS_NAMES):
                raise Exception('Unexpected class id %d - model does not match' % class_id)
            class_name = CLASS_NAMES[class_id]

            if (min_y - 5) > width and class_name == 'paragraph':
                min_y -= 5
            if (max_y + 10) < width and class_name == 'paragraph':
                min_y += 10

            # estimate glyph scale (roughly)
            mask = r['masks'][:,:,i]
            area = np.count_nonzero(mask)
            scale = int(np.sqrt(area)//10)
            scale = scale + (scale+1)%2 # odd

            # dilate mask until we have a single outer contour
            contours = [None, None]
            for _ in range(10):
                if len(contours) == 1:
                    break
                mask = cv2.dilate(mask.astype(np.uint8),
                                  np.ones((scale,scale), np.uint8)) > 0
                contours, _ = cv2.findContours(mask.astype(np.uint8),
                                               cv2.RETR_EXTERNAL,
                                               cv2.CHAIN_APPROX_SIMPLE)
            region_polygon = contours[0][:,0,:] # already in x,y order
            #region_polygon = [[min_y, min_x], [max_y, min_x], [max_y, max_x], [min_y, max_x]]

            # convert to absolute coordinates
            region_polygon = coordinates_for_segment(region_polygon, page_image, page_xywh)
            # intersect with parent and plausibilize
            cut_region_polygon = Polygon(region_polygon)
            if border:
                cut_region_polygon = border.intersection(cut_region_polygon)
            if cut_region_polygon.is_empty:
                LOG.warning('region %d does not intersect page frame', i)
                continue
            if not cut_region_polygon.is_valid:
                LOG.warning('region %d has invalid polygon', i)
                continue
            region_polygon = [j for j in zip(list(cut_region_polygon.exterior.coords.xy[0]),
                                                 list(cut_region_polygon.exterior.coords.xy[1]))][:-1]
            region_points = points_from_polygon(region_polygon)
            read_order = reading_order.index((min_y, min_x, max_y, max_x))
            region_args = {'custom': 'readingOrder {index:'+str(read_order)+';}',
                           'id': 'region%04d' % i,
                           'Coords': CoordsType(region_points)}
            if class_name == 'image':
                image_region = ImageRegionType(**region_args)
                page.add_ImageRegion(image_region)
            elif class_name == 'table':
                table_region = TableRegionType(**region_args)
                page.add_TableRegion(table_region)
            elif class_name == 'graphics':
                graphic_region = GraphicRegionType(**region_args)
                page.add_GraphicRegion(graphic_region)
            else:
                region_args['type_'] = class_name
                textregion = TextRegionType(**region_args)
                page.add_TextRegion(textregion)
            order_index = reading_order.index((min_y, min_x, max_y, max_x))
            regionRefIndex = RegionRefIndexedType(index=order_index, regionRef=region_args['id'])
            order_group.add_RegionRefIndexed(regionRefIndex)
            LOG.info('added %s region on page "%s"', class_name, page_id)


@click.command()
@ocrd_cli_options
def cli(*args, **kwargs):
    return ocrd_cli_wrap_processor(OcrdAnybaseocrBlockSegmenter, *args, **kwargs)
