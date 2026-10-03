"""行内矢量数学的槽位、上下标和正文连续性正反例。"""

import pytest

from test_flash_review_20261003 import _metric_fixture_line
from docvortex.document.pdf._document import PDFPathInfo
from docvortex.analyzers.native.pdf.models import _TextLane
from docvortex.analyzers.native.pdf.formulas import (
    _VectorPathComponent, _VectorFormulaCandidate, _inline_vector_formula_prose_sources, _join_open_vector_equation_rows,
)
from docvortex.analyzers.native.pdf.text_assembly.continuity import group_native_inline_formula_prose


def _path(box, index, segments=50, color=(0,0,0,255)):
    """构造有原生可见填充的矢量字形，不提供文件、页码或特定公式内容。"""
    return PDFPathInfo(box,segments,True,False,0,index,fill_rgba=color)


@pytest.mark.parametrize('scale,left',[(.7,20),(1,50),(1.6,90)])
@pytest.mark.parametrize('kind',['wide','script','both_scripts','plain_glyphs','colored','container','no_hosts','far_hosts','tall','short_prose','large_gap'])
def test_inline_vector_math_requires_complexity_script_or_wide_cluster_and_native_prose_slots(scale,left,kind):
    """字号变化下保留复杂公式及上下标，普通字形、彩色装饰和缺失槽位的图形不能当成行内数学。"""
    h=10*scale;top=100*scale
    start=left+100*scale
    if kind in {'script','both_scripts','plain_glyphs'}:
        if kind=='both_scripts':
            boxes=[(start,top,start+5*scale,top+5*scale),(start,top+8*scale,start+5*scale,top+13*scale)]
        else:
            boxes=[(start,top,start+6*scale,top+9*scale),
                   (start+7*scale,top+(0 if kind=='plain_glyphs' else 5)*scale,start+12*scale,top+(9 if kind=='plain_glyphs' else 10)*scale)]
    else:
        boxes=[(start+i*4*scale,top,start+(i+1)*4*scale,top+(25 if kind=='tall' else 10)*scale) for i in range(9)]
    bounds=(min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes))
    paths=[_path(box,index,color=(180,0,0,255) if kind=='colored' else (0,0,0,255)) for index,box in enumerate(boxes)]
    first=_metric_fixture_line('brief' if kind=='short_prose' else 'Different ordinary prose words before the expression',
                               (left,top,start-(20 if kind=='far_hosts' else 4)*scale,top+h),0,
                               effective_height=h,font_signature=('Body',0))
    second=_metric_fixture_line('brief' if kind=='short_prose' else ', ordinary words following the expression',
                                (bounds[2]+(20 if kind=='large_gap' else 4)*scale,top,left+300*scale,top+h),1,
                                effective_height=h,font_signature=('Body',0))
    lines=[] if kind=='no_hosts' else [(first,first.bbox),(second,second.bbox)]
    lane=_TextLane(left,left+300*scale,lines)
    component=_VectorPathComponent(0,paths,bounds)
    result=_inline_vector_formula_prose_sources(component,lane,h,[bounds] if kind=='container' else [])
    assert bool(result)==(kind in {'wide','script','both_scripts'})
    if result:
        assert result=={0,1}


@pytest.mark.parametrize('scale,left',[(.7,20),(1,50),(1.6,90)])
@pytest.mark.parametrize('kind',['equation','numbered','no_flat_glyph','off_center','tall_glyph','far','misaligned','not_wide_rhs','body','different_lane'])
def test_multiline_vector_equation_requires_short_open_lhs_centered_flat_operator_and_unblocked_long_rhs(scale,left,kind):
    """扁平末运算字形、缩进、净空和长度共同确认两行等式，编号或中间正文阻止合并。"""
    h=10*scale
    a=(left,100*scale,left+100*scale,112*scale)
    yy=(140 if kind=='far' else 122)*scale
    b=(left+(20*scale if kind=='misaligned' else 0),yy,left+(180 if kind=='not_wide_rhs' else 350)*scale,yy+12*scale)
    first=_VectorFormulaCandidate(0,a,{0},has_number=kind=='numbered')
    second=_VectorFormulaCandidate(1 if kind=='different_lane' else 0,b,{1})
    glyph=(left+92*scale,(100 if kind=='off_center' else 104.5)*scale,left+100*scale,
           (111 if kind=='tall_glyph' else 102 if kind=='off_center' else 107.5)*scale)
    if kind=='no_flat_glyph':
        glyph=(left+94*scale,100*scale,left+100*scale,112*scale)
    components=[_VectorPathComponent(0,[_path(glyph,0)],a),_VectorPathComponent(0,[_path(b,1)],b)]
    rows=[_metric_fixture_line('Unrelated intervening body',(left,113*scale,left+250*scale,121*scale),0)] if kind=='body' else []
    candidates=[first,second]
    _join_open_vector_equation_rows(candidates,components,rows,h)
    assert (len(candidates)==1)==(kind=='equation')
    if kind=='equation':
        assert candidates[0].bbox==pytest.approx((left,100*scale,left+350*scale,134*scale))
        assert candidates[0].path_source_indices=={0,1}


@pytest.mark.parametrize('scale,left',[(.7,20),(1,50),(1.6,90)])
@pytest.mark.parametrize('kind',['body','single_region','two_rows','few_words','font_reset','gap','different_column','marked','different_scale'])
def test_two_inline_native_formula_slots_group_only_continuous_same_style_prose(scale,left,kind):
    """至少两处公式和三排同式自然语言才锁定段组，字体重置、栏变、标题及留白均须分段。"""
    h=10*scale;rows=[]
    for index in range(2 if kind=='two_rows' else 4):
        x=left+(180*scale if kind=='different_column' and index>=2 else 0)
        y=(50+index*13+(20 if kind=='gap' and index>=2 else 0))*scale
        row=_metric_fixture_line('Small phrase' if kind=='few_words' else 'Different ordinary prose contains several useful explanatory words in this physical row',
                                  (x,y,x+150*scale,y+h),index,effective_height=h*(1.5 if kind=='different_scale' and index>=2 else 1),
                                  font_signature=('Other' if kind=='font_reset' and index>=2 else 'Body',0),
                                  visual_row_id=index,semantic_type='paragraph_title' if kind=='marked' and index>=2 else None)
        rows.append(row)
    regions=[((left+151*scale,50*scale,left+170*scale,60*scale),{0})]
    if kind!='single_region':
        row=rows[-1];regions.append(((row.bbox[2]+scale,row.bbox[1],row.bbox[2]+20*scale,row.bbox[3]),{row.source_index}))
    group_native_inline_formula_prose(rows,regions)
    assert any(row.paragraph_group is not None for row in rows)==(kind=='body')
    if kind=='body':
        assert len({row.paragraph_group for row in rows})==1 and all(row.semantic_type=='text' for row in rows)
