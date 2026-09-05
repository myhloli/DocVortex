"""PDF 页面坐标和裁图的公共纯函数接口。"""
from .visual_geometry import (
    _normalize_page_size as normalize_page_size,
    _bbox_to_pixel_bbox as bbox_to_pixel_bbox,
    _normalize_layout_bbox_to_unit as normalize_layout_bbox_to_unit,
    _medium_bbox_to_quad as medium_bbox_to_quad,
    _normalize_medium_content as normalize_medium_content,
    _table_bbox_center as table_bbox_center,
    _normalize_visual_block_angle as normalize_visual_block_angle,
    _rotate_visual_block_image_to_upright as rotate_visual_block_image_to_upright,
    _rotate_medium_table_bbox as rotate_medium_table_bbox,
    _get_medium_table_virtual_image_bbox as get_medium_table_virtual_image_bbox,
    _encode_page_crop_as_jpeg_data_uri as encode_page_crop_as_jpeg_data_uri,
    _sidecar_bbox_to_page_bbox as sidecar_bbox_to_page_bbox,
)

__all__ = ['normalize_page_size', 'bbox_to_pixel_bbox', 'normalize_layout_bbox_to_unit', 'medium_bbox_to_quad', 'normalize_medium_content', 'table_bbox_center', 'normalize_visual_block_angle', 'rotate_visual_block_image_to_upright', 'rotate_medium_table_bbox', 'get_medium_table_virtual_image_bbox', 'encode_page_crop_as_jpeg_data_uri', 'sidecar_bbox_to_page_bbox']
