"""改变原生字形尺度、位置和无关文字，验证本轮图注成员与公共父关系。"""

import pytest

from test_flash_review_20261003 import _metric_fixture_line
from docvortex.analyzers.native.pdf.models import _PageSource, _AxisLine
from docvortex.analyzers.native.pdf.graphics import _framed_side_caption_members, _graphic_members_are_numeric_grid
from docvortex.analyzers.native.pdf import visual_annotations as annotations


@pytest.mark.parametrize('scale,left', [(.7, 15), (1, 50), (1.6, 100)])
@pytest.mark.parametrize('kind', ['caption', 'far_image', 'not_bare', 'no_frame', 'font', 'few', 'numeric', 'inside', 'semantic'])
def test_framed_side_caption_requires_photo_bare_number_descriptive_rows_and_a_complete_frame(scale, left, kind):
    """图旁卡片需要编号、同式说明和完整细框，缺少任一证据不能把真正图表标签改成图注。"""
    h = 10 * scale
    caption = _metric_fixture_line('Figure 4. A graph' if kind == 'not_bare' else 'Figure 4',
                                  (left+120*scale,100*scale,left+160*scale,110*scale),0,
                                  effective_height=h,font_signature=('Card',0))
    lines = [caption]
    texts = ['Organisation description', 'Content health services', 'Community celebration event', 'Annual public event.']
    for index, text in enumerate(texts[:2] if kind == 'few' else texts):
        y = (85+12*index)*scale
        lines.append(_metric_fixture_line(str(index*23) if kind == 'numeric' else text,
                                          (left+180*scale,y,left+300*scale,y+h),index+1,effective_height=h,
                                          font_signature=('Other' if kind == 'font' and index==1 else 'Card',0),
                                          semantic_type='equation' if kind == 'semantic' else None))
    frame = (left+115*scale,82*scale,left+305*scale,131*scale)
    x0,y0,x1,y1 = frame
    rules = [_AxisLine((x0,y0,x1,y0),.5*scale,'horizontal'),_AxisLine((x0,y1,x1,y1),.5*scale,'horizontal'),
             _AxisLine((x0,y0,x0,y1),.5*scale,'vertical'),_AxisLine((x1,y0,x1,y1),.5*scale,'vertical')]
    image_right = 30 if kind == 'far_image' else 320 if kind == 'inside' else 90
    source = _PageSource((left+400*scale,300*scale),lines,[],[] if kind=='no_frame' else rules,
                         image_bboxes=[(left,30*scale,left+image_right*scale,210*scale)])
    result = _framed_side_caption_members(source,caption)
    assert bool(result) == (kind == 'caption')
    if result:
        assert [line.source_index for line in result] == [0,1,2,3,4]


@pytest.mark.parametrize('scale,left', [(.7, 15), (1, 50), (1.6, 100)])
@pytest.mark.parametrize('kind', ['grid','three_rows','two_columns','axis','shifted','prose'])
def test_numeric_grid_evidence_requires_repeated_two_dimensional_cells(scale,left,kind):
    """重复数字网格与图轴、普通正文、少量数字和逐排漂移的数据必须区分。"""
    lines=[]
    for row in range(3 if kind=='three_rows' else 5):
        for column in range(2 if kind=='two_columns' else 3):
            x=left+(column*70+(row*35 if kind=='shifted' else 0))*scale
            y=(50+row*20+(column*20 if kind=='axis' else 0))*scale
            lines.append(_metric_fixture_line('This is ordinary prose' if kind=='prose' else str(row*column+12),
                                              (x,y,x+20*scale,y+10*scale),len(lines),effective_height=10*scale))
    assert _graphic_members_are_numeric_grid(lines) == (kind=='grid')


@pytest.mark.parametrize('scale,left', [(.7,15),(1,50),(1.6,100)])
@pytest.mark.parametrize('kind', ['caption','regular','no_body','close_body','far_image','multiline','misaligned','short_image'])
def test_unlabelled_caption_needs_italic_single_row_large_photo_and_body_separation(scale,left,kind):
    """未编号说明的几何及斜体证据必须齐全，普通正文和紧接续行不能改成图注。"""
    h=10*scale
    capbox=(left+(30*scale if kind=='misaligned' else 0),111*scale,left+210*scale,121*scale)
    caption={'type':'text','bbox':capbox,'content':'A conceptual illustration of the topic shown in the photograph',
             '_font_signatures':{('Body',0 if kind=='regular' else 64)},'_line_heights':[h],
             '_local_line_bboxes':[capbox,capbox] if kind=='multiline' else [capbox]}
    photo={'type':'image','bbox':(left,90*scale if kind=='short_image' else 15*scale,
                                left+250*scale,85*scale if kind=='far_image' else 110*scale),'content':''}
    yy=(124 if kind=='close_body' else 150)*scale
    body={'type':'text','bbox':(left,yy,left+250*scale,yy+40*scale),
          'content':'Following body contains enough ordinary words to establish a separate paragraph that continues with complete natural language sentences.'}
    blocks=[photo,caption] if kind=='no_body' else [photo,caption,body]
    assert annotations._is_adjacent_unlabelled_italic_caption(1,blocks)==(kind=='caption')


@pytest.mark.parametrize('scale,left', [(.7,15),(1,50),(1.6,100)])
@pytest.mark.parametrize('kind',['evidence','none','missing','ambiguous','barrier'])
def test_internal_native_parent_evidence_is_consumed_only_for_one_valid_visual_neighbor(scale,left,kind):
    """内部父框只匹配合法邻图；缺失和正文屏障回落距离，后面的重复图由邻接屏障排除。"""
    from docvortex.postprocess.visual import find_best_visual_parent
    first={'type':'image','bbox':(left,20*scale,left+200*scale,100*scale),'index':0}
    caption={'type':'caption','bbox':(left+30*scale,120*scale,left+160*scale,130*scale),'index':1}
    second={'type':'image','bbox':(left,170*scale,left+200*scale,250*scale),'index':2}
    if kind!='none':
        caption['_native_annotation_parent_bbox']=(left,370*scale,left+200*scale,450*scale) if kind=='missing' else second['bbox']
    blocks=[first,caption,second]
    mains=[first,second]
    if kind=='ambiguous':
        duplicate={**second,'index':3};mains.append(duplicate);blocks.append(duplicate)
    if kind=='barrier':
        barrier={'type':'text','bbox':(left,140*scale,left+200*scale,160*scale),'index':2,'content':'Intervening body paragraph'}
        second['index']=3;blocks=[first,caption,barrier,second]
    result=find_best_visual_parent(caption,mains,blocks,{block['index']:i for i,block in enumerate(blocks)})
    assert result is (second if kind in {'evidence','ambiguous'} else first)


@pytest.mark.parametrize('scale,left', [(.7,15),(1,50),(1.6,100)])
@pytest.mark.parametrize('kind',['graph','ordinary','no_grid_evidence','no_other_image','body_barrier'])
def test_graph_caption_keeps_native_grid_exclusion_through_legacy_decoration_band(scale,left,kind):
    """图题词义加重复网格证据才胜过旧装饰图注带；普通图题、单图和正文屏障维持近距绑定。"""
    upper={'type':'image','bbox':(left,20*scale,left+220*scale,100*scale),'content':'Numeric cell grid',
           '_native_numeric_grid':kind!='no_grid_evidence'}
    lower={'type':'image','bbox':(left,160*scale,left+220*scale,240*scale),'content':''}
    caption={'type':'text','bbox':(left+25*scale,115*scale,left+190*scale,130*scale),
             'content':'Figure 4. A useful diagram' if kind=='ordinary' else 'Figure 4. Graph of changes',
             '_line_heights':[10*scale], '_annotation_band_parent':upper}
    blocks=[upper,caption] if kind=='no_other_image' else [upper,caption,lower]
    if kind=='body_barrier':
        blocks.append({'type':'text','bbox':(left,140*scale,left+220*scale,150*scale),'content':'Ordinary intervening prose'})
    regions=annotations._classify_and_bind_visual_annotations(blocks,(left+300*scale,300*scale))
    target=lower if kind=='graph' else upper
    assert any(caption in region and target in region for region in regions)
    assert caption.get('_native_annotation_parent_bbox')==(lower['bbox'] if kind=='graph' else None)
