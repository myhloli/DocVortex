"""原生标题续行、跨栏项目和图旁段尾的尺度与否定证据。"""

import pytest

from test_flash_review_20261003 import _metric_fixture_line
from docvortex.analyzers.native.pdf import pipeline
from docvortex.analyzers.native.pdf.title_analysis.structural import (
    _restore_wrapped_bold_title_tails, _restore_short_heading_with_image_displaced_prose,
)
from docvortex.analyzers.native.pdf.text_assembly.rows import _repeated_bullet_break_sources
from docvortex.analyzers.native.pdf.line_layout import _font_signatures_share_emphasis_family
from docvortex.analyzers.native.pdf.line_merging import _merge_same_baseline_group


def _native_chars(text, left, top, height, prefix_count=0, prefix_weight=700, regular_weight=400):
    """构造不同字重的原生字符，字形坐标随字号同步改变。"""
    chars = []
    for index, letter in enumerate(text):
        bold = index < prefix_count
        chars.append({'char': letter, 'bbox': (left+index*.5*height,top,left+(index+1)*.5*height,top+height),
                      'font': {'name': 'Example-Bold' if bold else 'Example-Regular', 'flags': 0,
                               'weight': prefix_weight if bold else regular_weight, 'size': height}})
    return chars


@pytest.mark.parametrize('scale,left', [(.7,20),(1,50),(1.6,90)])
@pytest.mark.parametrize('kind', ['title','numbered','closed','ordinary','far','offset','font','regular_prefix','bold_body','long_prefix','caption'])
def test_short_native_bold_prefix_returns_only_to_an_open_matching_heading_after_character_release(scale,left,kind):
    """字符释放后仍能回收同式短粗体标题末词；正文、异式标题和区域偏移必须保留。"""
    h=10*scale
    prefix='A much longer ending.' if kind=='long_prefix' else 'ending.'
    text=prefix+' Another ordinary sentence continues over the next physical row'
    mixed=_metric_fixture_line(text,(left,112*scale,left+300*scale,122*scale),2,
                               effective_height=h,font_signature=('Example-Regular',0),
                               chars=_native_chars(text,left,112*scale,h,len(prefix),
                                                   400 if kind=='regular_prefix' else 700,
                                                   700 if kind=='bold_body' else 400),
                               caption_start=kind=='caption')
    title=_metric_fixture_line('A complete heading.' if kind=='closed' else 'A different heading with an open',
                               (left+(20*scale if kind=='offset' else 0),100*scale,left+200*scale,110*scale),1,
                               chars=_native_chars('A different heading with an open',left,100*scale,h,99),
                               effective_height=h,font_signature=('Different' if kind=='font' else 'Example-Bold',0),
                               dominant_font_weight=700,semantic_type=None if kind=='ordinary' else 'paragraph_title',
                               paragraph_terminal=kind=='closed')
    if kind=='far':
        mixed.bbox=(left,140*scale,left+300*scale,150*scale)
        mixed.chars=_native_chars(text,left,140*scale,h,len(prefix))
    continuation=_metric_fixture_line('Following unrelated prose concludes the ordinary explanation.',
                                      (left,124*scale,left+280*scale,134*scale),3,
                                      effective_height=h,font_signature=('Example-Regular',0))
    rows=[title,mixed,continuation]
    page_size=(left+1000*scale,300*scale)
    pipeline._compact_prepared_lines(rows,page_size)
    assert all(not line.chars for line in rows)
    if kind=='numbered':
        marker=_metric_fixture_line('b.',(left-20*scale,100*scale,left-12*scale,110*scale),0,
                                    font_signature=title.font_signature,effective_height=h,
                                    dominant_font_weight=700,semantic_type='paragraph_title')
        title=_merge_same_baseline_group([0,1],[marker,title],[marker.bbox,title.bbox],page_size)
        rows[0]=title
    result=_restore_wrapped_bold_title_tails(rows,page_size)
    expected=kind in {'title','numbered'}
    assert (len(result)==4)==expected
    if expected:
        assert result[-1].text==prefix and result[-1].semantic_type=='paragraph_title'
        assert mixed.text.startswith('Another ordinary') and mixed.paragraph_group==continuation.paragraph_group
        assert title.title_band_id==result[-1].title_band_id
    else:
        assert mixed.text==text


@pytest.mark.parametrize('scale,left', [(.7,20),(1,50),(1.6,90)])
@pytest.mark.parametrize('kind', ['continuation','no_image','small_image','overlap_tail','terminal','font','far','long_tail','misaligned','marked'])
def test_image_displaced_short_sentence_tail_requires_complete_native_continuity(scale,left,kind):
    """连续正文和图像右缩共同证明短段尾，完整收句、异式文字和重叠图内标签不能回接。"""
    h=10*scale
    heading=_metric_fixture_line('Example open heading',(left,60*scale,left+150*scale,70*scale),0,
                                 effective_height=h,font_signature=('Body',0))
    first=_metric_fixture_line('The ordinary explanation contains many different useful words here',
                               (left,90*scale,left+300*scale,100*scale),1,effective_height=h,font_signature=('Body',0))
    second=_metric_fixture_line('Another ordinary row continues the explanation with more different useful words'+('.' if kind=='terminal' else ''),
                                (left,102*scale,left+300*scale,112*scale),2,effective_height=h,font_signature=('Body',0),
                                paragraph_terminal=kind=='terminal')
    tail=_metric_fixture_line('several words form a longer complete sentence.' if kind=='long_tail' else 'choice.',
                              (left+(30*scale if kind=='misaligned' else 0),220*scale,left+35*scale,230*scale),3,
                              effective_height=h,font_signature=('Other' if kind=='font' else 'Body',0),
                              semantic_type='caption' if kind=='marked' else None)
    image=(left if kind=='overlap_tail' else left+40*scale,115*scale,
           left+300*scale,(150 if kind=='small_image' else 190 if kind=='far' else 230)*scale)
    rows=[heading,first,second,tail]
    _restore_short_heading_with_image_displaced_prose(rows,[] if kind=='no_image' else [image])
    expected=kind=='continuation'
    assert (first.paragraph_group is not None)==expected
    if expected:
        assert first.paragraph_group==second.paragraph_group==tail.paragraph_group
        assert heading.semantic_type=='paragraph_title'
    else:
        assert heading.semantic_type is None and tail.paragraph_group is None


@pytest.mark.parametrize('scale,left', [(.7,20),(1,50),(1.6,90)])
@pytest.mark.parametrize('kind', ['cross_column','no_colon','single_peer','font','offset_peers','far_intro','indent','marked'])
def test_cross_column_first_bullet_needs_colon_introduction_and_repeated_right_column_peers(scale,left,kind):
    """另一栏重复圆点只在同式冒号引导证据齐全时分开首条，孤立或不齐的项目保持原判。"""
    h=10*scale
    intro=_metric_fixture_line('Different ordinary choices were'+('.' if kind=='no_colon' else ':'),
                               (left,80*scale,left+190*scale,90*scale),0,effective_height=h)
    yy=(150 if kind=='far_intro' else 100)*scale
    bullet=_metric_fixture_line('• A first useful item',(left+(40*scale if kind=='indent' else 10*scale),yy,left+180*scale,yy+h),1,
                                effective_height=h,font_signature=('Body',0),semantic_type='caption' if kind=='marked' else None)
    peers=[]
    for index in range(1 if kind=='single_peer' else 2):
        x=left+(220+(40*index if kind=='offset_peers' else 0))*scale
        peers.append(_metric_fixture_line('• Another useful item',(x,(50+20*index)*scale,x+100*scale,(60+20*index)*scale),index+2,
                                           effective_height=h,font_signature=('Other' if kind=='font' else 'Body',0)))
    result=_repeated_bullet_break_sources([(line,line.bbox) for line in [intro,bullet,*peers]])
    assert (bullet.source_index in result)==(kind=='cross_column')


@pytest.mark.parametrize('first,second,expected', [('Example-Roman','Example-Italic',True),
                                                  ('Example-Regular','Example-BoldItalic',True),
                                                  ('Example-Roman','Different-Italic',False),
                                                  ('Example-Roman','Example-Display',False),
                                                  ('Roman','Italic',False)])
def test_emphasis_family_matches_generic_roman_style_without_accepting_unrelated_faces(first,second,expected):
    """强调续行可忽略通用Roman样式后缀，字体族和未知样式仍然构成边界。"""
    assert _font_signatures_share_emphasis_family((first,0),(second,64))==expected
