"""中文段界、原生条目、短题名与编号面板网格的尺度和反例验证。"""

import pytest

from test_flash_review_20261003 import _metric_fixture_line
from docvortex.analyzers.native.pdf.models import _TextLane
from docvortex.analyzers.native.pdf.text_assembly.rows import _cjk_prose_break_sources, _cjk_entry_break_sources
from docvortex.analyzers.native.pdf.title_analysis.structural import _classify_short_cjk_section_leads
from docvortex.analyzers.native.pdf.pipeline import _numbered_panel_row_regions


@pytest.mark.parametrize('scale,left,width', [(.7,15,260),(1,55,360),(1.6,110,500)])
@pytest.mark.parametrize('kind', ['paragraph','label','same_gap','open','foreign','font','offset','member','caption','short'])
def test_cjk_sentence_and_extra_native_space_confirm_paragraph_boundary_without_splitting_wraps(scale,left,width,kind):
    """变换字号、栏宽与位置后中文句末仍由额外净空分段，普通续行和不完整句不拆。"""
    h=10*scale
    first=_metric_fixture_line('前一段经过完整说明后结束。' if kind!='open' else '前一段还有未完成的说明',
                               (left,100*scale,left+.85*width,110*scale),1,effective_height=h,font_coverage=1,font_signature=('GenericA',0))
    text='后续正文介绍另一组相关测量事实及结果。'
    if kind=='label': text='处理建议：后续说明仍然保持完整。'
    if kind=='foreign': text='additional explanation continues here.'
    if kind=='short': text='结论。'
    gap=(4 if kind=='same_gap' else 6.5 if kind=='label' else 8)*scale
    current=_metric_fixture_line(text,(left+(5*scale if kind=='offset' else 0),110*scale+gap,left+width,120*scale+gap),2,
                                 effective_height=h,font_coverage=1,font_signature=('GenericB' if kind=='font' else 'GenericA',0),
                                 paragraph_group=9 if kind=='member' else None,caption_start=kind=='caption')
    lane=_TextLane(left,left+width,[(first,first.bbox),(current,current.bbox)])
    assert (2 in _cjk_prose_break_sources(lane,4*scale,0))==(kind in {'paragraph','label'})


@pytest.mark.parametrize('scale,left,width', [(.7,15,260),(1,55,360),(1.6,110,500)])
@pytest.mark.parametrize('kind', ['numbered','dated','two','nonsequential','offset','font','no_date','embedded'])
def test_repeated_cjk_entries_require_stable_native_starts_and_structural_sequence_or_dates(scale,left,width,kind):
    """连续顿号编号或带日期书名号才提供条目边界，段内引用、缺日期及不齐列不能认领。"""
    rows=[]; h=10*scale
    count=2 if kind=='two' else 3
    dated=kind in {'dated','no_date'}
    for index in range(count):
        y=(100+28*index)*scale
        number=index+1+(1 if kind=='nonsequential' and index==1 else 0)
        text='《另一家公司年度经营表现及行业分析' if dated else f'{number}、指标调整：本项说明需要保持独立完整'
        if kind=='embedded': text='段内说明提到'+text
        x=left+(10*scale*index if kind=='offset' else 0)
        first=_metric_fixture_line(text,(x,y,left+width,y+h),2*index+1,effective_height=h,font_coverage=1,
                                   font_signature=('GenericB' if kind=='font' and index==1 else 'GenericA',0))
        tail='后续正文继续说明这项调整。'
        if dated: tail='完整结论》 ——2020-05-16' if kind!='no_date' else '完整结论》并不包含日期'
        second=_metric_fixture_line(tail,(x,y+14*scale,left+.6*width,y+24*scale),2*index+2,effective_height=h,
                                    font_signature=first.font_signature)
        rows.extend([first,second])
    lane=_TextLane(left,left+width,[(line,line.bbox) for line in rows])
    assert bool(_cjk_entry_break_sources(lane))==(kind in {'numbered','dated'})


@pytest.mark.parametrize('scale,left,width', [(.7,15,260),(1,55,360),(1.6,110,500)])
@pytest.mark.parametrize('kind', ['heading','dated','nested','tail','colon','long','far','small','font','caption'])
def test_isolated_short_cjk_heading_requires_two_native_body_rows_and_no_nearby_predecessor(scale,left,width,kind):
    """短中文题名只在独立章节起点晋升；段尾、冒号引导和异式正文不充当标题。"""
    h=10*scale
    text='测量结果说明'
    if kind=='colon': text+='：'
    if kind=='long': text='这是一段很长的普通正文说明不应该成为短题名'
    head=_metric_fixture_line(text,(left,100*scale,left+60*scale,110*scale),1,effective_height=5*scale if kind=='small' else h,
                              font_coverage=1,font_signature=('GenericA',0),caption_start=kind=='caption')
    y=(130 if kind=='far' else 113)*scale
    first=_metric_fixture_line('不同市场环境中的结果分析需要保持完整并与相关内容相互验证。',
                               (left,y,left+width,y+h),2,effective_height=h,font_coverage=1,font_signature=('GenericB' if kind=='font' else 'GenericA',0))
    second=_metric_fixture_line('后续正文继续说明当前记录与其他内容的关系并保持自然续行。',
                                (left,y+14*scale,left+width,y+24*scale),3,effective_height=h,font_coverage=1,font_signature=first.font_signature)
    if kind=='dated':
        first.text='《不同市场经营情况与综合评估结果完整说明'
        second.text='完整结论》 ——2020-05-16'
    lines=[head,first,second]
    if kind in {'tail','nested'}:
        lines.insert(0,_metric_fixture_line('前一物理行的普通正文仍然处于这一段中',
                                           (left,86*scale,left+width,96*scale),0,effective_height=h,font_coverage=1,font_signature=('GenericA',0),
                                           semantic_type='paragraph_title' if kind=='nested' else None))
    _classify_short_cjk_section_leads(lines)
    assert (head.semantic_type=='paragraph_title')==(kind in {'heading','dated','nested'})


@pytest.mark.parametrize('scale,left,width', [(.7,15,260),(1,55,360),(1.6,110,500)])
@pytest.mark.parametrize('kind', ['grid','six','column_numbers','nonconsecutive','offset','body','below','single_row','single_column','missing'])
def test_numbered_panel_grid_uses_visual_rows_only_when_native_numbers_confirm_row_order(scale,left,width,kind):
    """面板位置和连续图号同时成立才逐行排序，非逐行编号、正文屏障及不齐列保留原分组。"""
    rows=3 if kind=='six' else 1 if kind=='single_row' else 4 if kind=='single_column' else 2
    columns=1 if kind=='single_column' else 2
    regions=[]; h=10*scale
    for row in range(rows):
        for column in range(columns):
            if kind=='missing' and row==rows-1 and column==columns-1: continue
            x=left+column*(width+25*scale)+(12*scale if kind=='offset' and row==1 and column==1 else 0)
            y=(100+130*row)*scale
            number=row*columns+column+1
            if kind=='column_numbers': number=column*rows+row+1
            if kind=='nonconsecutive' and row==1 and column==1: number+=2
            caption={'type':'caption','bbox':(x,y,x+.8*width,y+h),'content':f'图{number}：其他测量结果'}
            body={'type':'image','bbox':(x,y+15*scale,x+width,y+110*scale),'content':''}
            if kind=='below': caption['bbox']=(x,y+111*scale,x+.8*width,y+121*scale)
            regions.append([caption,body])
    blocks=[member for region in regions for member in region]
    if kind=='body': blocks.append({'type':'text','bbox':(left,180*scale,left+width,190*scale),'content':'正文屏障'})
    result=_numbered_panel_row_regions(regions,blocks)
    assert (result is not regions)==(kind in {'grid','six'})
    if kind in {'grid','six'}:
        assert len(result)==rows and all(len(region)==columns*2 for region in result)
