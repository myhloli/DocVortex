"""无框线混合数据表的表题、列、分组表头和单行续表证据。"""

import pytest

from test_flash_review_20261003 import _metric_fixture_line
from test_flash_review_paragraph_grounding import _native_chars
from docvortex.analyzers.native.pdf.models import _PageSource
from docvortex.analyzers.native.pdf.table_detection import (
    _detect_captioned_mixed_numeric_tables, _native_header_word_parts, _near_native_table_note,
    _detect_year_header_numeric_tables, _detect_ruled_metric_pair_tables, _prefer_complete_table_replacements,
)
from docvortex.analyzers.native.pdf.models import _AxisLine, _TableCandidate


@pytest.mark.parametrize('scale,left,width',[(.7,20,220),(1,50,280),(1.6,90,340)])
@pytest.mark.parametrize('kind',['table','stacked','amount_header','duplicate_year','reverse_year','no_rule','drift','few_rows','non_numeric','excluded'])
def test_year_header_table_requires_sequential_years_complete_numeric_rows_and_separate_stacked_headers(scale,left,width,kind):
    """改变位置、字号和栏宽验证独立年份表；现金金额、重复年份及不稳定列不能冒充表头。"""
    h=10*scale; lines=[]; rules=[]
    for offset in ([0,120] if kind=='stacked' else [0]):
        y=(50+offset)*scale
        for column in range(4):
            right=left+(width-10-(3-column)*55)*scale if column else left+50*scale
            label='独立项目' if column==0 else str(2100+column)
            if column and kind=='amount_header': label=str([0,3970,4862,4001][column])
            if column and kind=='duplicate_year': label='2101'
            if column and kind=='reverse_year': label=str(2104-column)
            lines.append(_metric_fixture_line(label,(right-(40 if column==0 else 25)*scale,y,right,y+h),len(lines),effective_height=h))
        if kind!='no_rule': rules.append(_AxisLine((left,y+1.1*h,left+width*scale,y+1.12*h),.2*scale,'horizontal'))
        for row in range(2 if kind=='few_rows' else 4):
            for column in range(4):
                right=left+(width-10-(3-column)*55)*scale if column else left+50*scale
                if kind=='drift' and row==1 and column: right+=10*scale
                label='其他指标' if column==0 else '文字说明' if kind=='non_numeric' and column==1 else str(20+row+column)
                top=y+(22+row*20)*scale
                lines.append(_metric_fixture_line(label,(right-(40 if column==0 else 20)*scale,top,right,top+h),len(lines),effective_height=h))
    source=_PageSource((left+500*scale,400*scale),lines,[],rules)
    result=_detect_year_header_numeric_tables(source,[(left,40*scale,left+width*scale,160*scale)] if kind=='excluded' else [])
    assert len(result)==(2 if kind=='stacked' else 1 if kind=='table' else 0)
    if result:
        assert all(len(item.line_indices)==20 and item.inferred_grid_authoritative for item in result)
        assert len(set.union(*(item.line_indices for item in result)))==20*len(result)


@pytest.mark.parametrize('scale,left,width',[(.7,20,220),(1,50,280),(1.6,90,340)])
@pytest.mark.parametrize('kind',['table','one_text','few_rows','no_rule','ordinary_title','drift','narrow_gap','text_values','excluded'])
def test_ruled_metric_pairs_require_repeated_labels_numeric_values_and_a_short_native_heading(scale,left,width,kind):
    """短题名加长横线和稳定两列方可成表；正文题名、窄缝或多数文字值保留文本。"""
    h=10*scale; lines=[_metric_fixture_line('独立基本数据' if kind!='ordinary_title' else 'Ordinary report title',
          (left,30*scale,left+60*scale,45*scale),0,effective_height=1.5*h)]
    rules=[] if kind=='no_rule' else [_AxisLine((left+60*scale,45*scale,left+width*scale,45.2*scale),.2*scale,'horizontal')]
    for row in range(4 if kind=='few_rows' else 6):
        top=(50+row*17)*scale
        drift=10*scale if kind=='drift' and row==2 else 0
        label_right=left+70*scale
        value_left=label_right+(5 if kind=='narrow_gap' else width-130)*scale
        value='独立说明' if kind=='text_values' or kind=='one_text' and row==0 else '12.5百万元'
        lines.append(_metric_fixture_line('其他指标',(left+drift,top,label_right+drift,top+h),len(lines),effective_height=h))
        lines.append(_metric_fixture_line(value,(value_left,top,left+width*scale,top+h),len(lines),effective_height=h))
    source=_PageSource((left+500*scale,300*scale),lines,[],rules)
    result=_detect_ruled_metric_pair_tables(source,[(left,45*scale,left+width*scale,150*scale)] if kind=='excluded' else [])
    assert bool(result)==(kind in {'table','one_text'})
    if result:
        assert len(result)==1 and len(result[0].line_indices)==12 and result[0].annotations[0].line_indices=={0}


@pytest.mark.parametrize('scale',[.7,1,1.6])
@pytest.mark.parametrize('kind',['complete','stacked','partial','duplicate_partial'])
def test_partial_table_detection_cannot_replace_or_double_claim_an_existing_complete_table(scale,kind):
    """局部年份行不得吞掉旧表；完整上下两张可替代长表，重复覆盖不能伪造完整面积。"""
    def candidate(bounds):
        """用明确范围构造待合并候选，保持几何缩放一致。"""
        box=tuple(value*scale for value in bounds)
        return _TableCandidate(box,box,0,14,core_bbox=box)
    old=candidate((10,20,210,220))
    new=[candidate((10,20,210,220))] if kind=='complete' else [candidate((10,20,210,115)),candidate((10,125,210,220))] if kind=='stacked' else [candidate((10,20,210,100))]
    if kind=='duplicate_partial': new.append(candidate((10,20,210,100)))
    base,recovered,replacements=_prefer_complete_table_replacements([old],[],new)
    if kind in {'complete','stacked'}:
        assert base==[] and replacements==new
    else:
        assert base==[old] and replacements==[]
    assert recovered==[]


@pytest.mark.parametrize('scale,left',[(.7,20),(1,50),(1.6,90)])
@pytest.mark.parametrize('kind',['table','group_header','units','no_caption','two_rows','drifting','overlapping','non_numeric','no_header','far_caption','excluded'])
def test_captioned_mixed_numeric_table_requires_repeated_columns_short_labels_and_independent_caption(scale,left,kind):
    """三列数值加短文字首列必须稳定且有独立表题，多层表头和单位不改变成员；位置扰动验证泛化。"""
    h=10*scale;rows=[]
    labels=['Place','First value','Second value']
    for column,(label,offset) in enumerate(zip(labels,[0,70,140])):
        y=(50 if kind=='group_header' and column==0 else 64)*scale
        rows.append(_metric_fixture_line(label if kind!='no_header' else 'Completed prose sentence.',
                                         (left+offset*scale,y,left+(offset+45)*scale,y+h),len(rows),
                                         effective_height=h,paragraph_terminal=kind=='no_header'))
    if kind=='group_header':
        rows.append(_metric_fixture_line('Combined values',(left+70*scale,50*scale,left+185*scale,60*scale),len(rows),effective_height=h))
    for row in range(2 if kind=='two_rows' else 4):
        for column in range(3):
            offset=[0,25 if kind=='overlapping' else 70,140][column]+(20 if kind=='drifting' and row==1 and column else 0)
            value='Natural label' if column==0 else 'Unknown value' if kind=='non_numeric' and column==1 else '*12 ml' if kind=='units' else str(20+row+column)
            y=(84+row*20)*scale
            rows.append(_metric_fixture_line(value,(left+offset*scale,y,left+(offset+40)*scale,y+h),len(rows),effective_height=h))
    yy=(210 if kind=='far_caption' else 169)*scale
    caption=_metric_fixture_line('Table 4. Example measurements' if kind!='no_caption' else 'Unrelated explanatory report',
                                 (left,yy,left+200*scale,yy+h),len(rows),effective_height=h)
    rows.append(caption)
    source=_PageSource((left+400*scale,300*scale),rows,[],[])
    result=_detect_captioned_mixed_numeric_tables(source,[(left,40*scale,left+220*scale,160*scale)] if kind=='excluded' else [])
    expected=kind in {'table','group_header','units'}
    assert bool(result)==expected
    if expected:
        assert len(result)==1 and len(result[0].line_indices)==(16 if kind=='group_header' else 15)
        assert result[0].annotations[0].line_indices=={caption.source_index}
        assert result[0].inferred_grid_authoritative


@pytest.mark.parametrize('scale,left',[(.7,20),(1,50),(1.6,90)])
@pytest.mark.parametrize('kind',['years','ranges','two','mixed','no_chars'])
def test_header_year_row_uses_native_glyph_positions_only_when_every_word_is_a_year_label(scale,left,kind):
    """年份行按原生字符定位独立列；两词、混合文字和缺字形的行不允许猜拆。"""
    text='2002-2006 2006-2010 2010-2014' if kind=='ranges' else '2002 2006' if kind=='two' else 'Ordinary 2006 2010' if kind=='mixed' else '2002 2006 2010'
    h=10*scale;chars=[] if kind=='no_chars' else _native_chars(text,left,50*scale,h)
    line=_metric_fixture_line(text,(left,50*scale,left+200*scale,60*scale),7,chars=chars,effective_height=h)
    result=_native_header_word_parts(line)
    assert (len(result)==3)==(kind in {'years','ranges'})
    assert all(part.source_index==7 for part in result)
    if len(result)==3:
        assert result[0].bbox[2]<result[1].bbox[0]<result[2].bbox[0]
        assert ' '.join(part.text for part in result)==text


@pytest.mark.parametrize('scale,left',[(.7,20),(1,50),(1.6,90)])
@pytest.mark.parametrize('kind',['source','star','chinese','numeric_star','ordinary','far','duplicate','following_header'])
def test_native_table_note_needs_clear_marker_and_proximity_and_stops_at_a_separated_header(scale,left,kind):
    """明确来源或星号自然语言才是表注，数值星号、普通正文、远距和歧义标记不能认领。"""
    h=10*scale
    text='*Change these quantities for a larger container.' if kind=='star' else '来源：独立来源和注释说明' if kind=='chinese' else '*12 ml' if kind=='numeric_star' else 'Ordinary explanation of the measurements.' if kind=='ordinary' else 'Source: Alternative collection of measurements'
    y=(140 if kind=='far' else 110)*scale
    note=_metric_fixture_line(text,(left,y,left+200*scale,y+h),0,font_signature=('Note',0),effective_height=h)
    rows=[note]
    if kind=='duplicate':
        rows.append(_metric_fixture_line('Source: Another independent source report',(left,115*scale,left+200*scale,125*scale),1,effective_height=h))
    if kind=='following_header':
        rows.append(_metric_fixture_line('Different table columns',(left,132*scale,left+200*scale,142*scale),1,font_signature=('Note',0),effective_height=h))
    result=_near_native_table_note(rows,(left,20*scale,left+210*scale,100*scale),h)
    assert bool(result)==(kind in {'source','star','chinese','following_header'})
    if result:
        assert result.line_indices=={0} and result.bbox==note.bbox
