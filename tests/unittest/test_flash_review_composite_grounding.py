"""两页最后必修项的原页视觉失败断言；只读取PDF原件，不读取GT。"""
from pathlib import Path
from bs4 import BeautifulSoup
from docvortex.analyzers.native.pdf.pipeline import _analyze_native_document
from docvortex.document.pdf import PDFDocument
from _flash_pdf_test_utils import _visible_text
from docvortex.analyzers.native.pdf.pipeline import _collect_document_sources
import pytest

PDFS=Path(__file__).parent/'pdfs/flash_review_20261003'


def test_two_composite_raster_charts_keep_complete_axis_category_and_legend_members():
    """原页两幅图必须各保留一份完整区域，轴标签、类别和图例归属自己的图，说明和脚注保持图外。"""
    with PDFDocument(str(PDFS/'review_199.pdf')) as pdf:
        page=_analyze_native_document(pdf)[0]
    images=sorted([b for b in page if b['type']=='image'],key=lambda b:b['bbox'][0])
    assert len(images)==2
    left,right=images
    assert left['bbox'][0]<=.0715 and left['bbox'][2]>=.408 and left['bbox'][3]>=.821
    assert right['bbox'][0]<=.520 and right['bbox'][2]>=.952 and right['bbox'][3]>=.814
    assert all(word in _visible_text(left['content']) for word in ['65','100','Scene','Document','70.23','95.5'])
    assert all(word in _visible_text(right['content']) for word in ['OCR-Recall','OCR-Precision','Parsing-F1','Company A','Company B','100'])
    assert all(b['bbox'][3]<.87 for b in images)
    assert any(b['type']=='page_footnote' for b in page)
    assert 'criteria' not in _visible_text(right['content'])
    assert '\n11\n' not in _visible_text(right['content'])
    description=next(b for b in page if b['type']=='paragraph_title' and 'performance details' in _visible_text(b['content']))
    assert _visible_text(description['content']).endswith('criteria')
    notes=[b for b in page if b['type']=='page_footnote']
    assert len(notes)==6
    assert [str(index) for index in range(1,7)]==sorted(_visible_text(b['content']).split()[0].rstrip('.') for b in notes)


def test_multiline_service_table_keeps_four_columns_and_native_stage_rowspans():
    """原页功能表为十行四列，第二阶段跨三行、第四阶段跨四行，不能退回纯文本表或填充空白图片。"""
    with PDFDocument(str(PDFS/'review_200.pdf')) as pdf:
        page=_analyze_native_document(pdf)[0]
    tables=[b for b in page if b['type']=='table']
    assert len(tables)==1
    soup=BeautifulSoup(tables[0]['content'],'html.parser')
    assert soup.find('table')
    rows=soup.find_all('tr')
    assert len(rows)==10
    assert [cell.get_text(' ',strip=True) for cell in rows[0].find_all(['th','td'])]==['Service Stage','Function Name','Explanation','Expected Benefit']
    stages=[cell for cell in soup.find_all(['th','td']) if cell.get_text(' ',strip=True).startswith(('1. Project','2. Data','3. Pipeline','4. Monitoring'))]
    assert len(stages)==4
    assert [int(cell.get('rowspan',1)) for cell in stages]==[1,3,1,4]
    assert not any(b['type']=='image' and b['bbox'][1]>.2 for b in page)


@pytest.mark.parametrize('name,noise',[('review_198',{'6'}),('review_199',{'11'}),('review_200',{'3','5'})])
def test_isolated_faint_native_digit_overlays_do_not_enter_directory_or_table_members(name,noise):
    """198的浅灰6和200的浅灰3、5为叠入大文字区域的噪声，原生行与表格字符成员同步排除。"""
    with PDFDocument(str(PDFS/f'{name}.pdf')) as pdf:
        source=_collect_document_sources(pdf).page_sources[0]
    assert not any(line.text.strip() in noise for line in source.lines)
    region=(348,193,352,199) if name=='review_198' else (345,192,352,199) if name=='review_199' else (347,188,352,202)
    assert not any(char.get('char') in noise and region[0] <= char['bbox'][0] <= region[2]
                   and region[1] <= char['bbox'][1] <= region[3] for char in source.chars)
    if name=='review_198':
        assert len([line for line in source.lines if line.text.startswith(('1.','2.','3.','4.','5.'))])==5
    elif name=='review_200':
        assert any('information comparison between' in line.text for line in source.lines)
    else:
        assert any(line.text.strip()=='95' for line in source.lines)


@pytest.mark.parametrize('scale,left,width',[(.7,20,160),(1,60,210),(1.6,110,280)])
@pytest.mark.parametrize('vertical',[True,False])
@pytest.mark.parametrize('kind',['chart','no_raster','inside','irregular_values','irregular_spacing','few','far','large_title'])
def test_native_raster_axis_requires_repeated_linear_ticks_and_nearby_matching_plot(scale,left,width,vertical,kind):
    """移动字号和图宽，等差等距刻度必须位于贴邻图外；内嵌数字、任意序列和远距图片不能成图。"""
    from test_flash_review_20261003 import _metric_fixture_line
    from docvortex.analyzers.native.pdf.models import _PageSource
    from docvortex.analyzers.native.pdf.graphics import _detect_native_raster_axis_graphics
    h=6*scale;image=(left,70*scale,left+width*scale,190*scale);lines=[]
    for index in range(4 if kind=='few' else 7):
        number=str(10+5*index+(1 if kind=='irregular_values' and index==2 else 0))
        if vertical:
            x=left+(5 if kind=='inside' else -45 if kind=='far' else -12)*scale
            y=(72+19*index+(4 if kind=='irregular_spacing' and index==2 else 0))*scale
        else:
            x=left+(4+index*(width-8)/6+(4 if kind=='irregular_spacing' and index==2 else 0))*scale
            y=(183 if kind=='inside' else 220 if kind=='far' else 196)*scale
        lines.append(_metric_fixture_line(number,(x,y,x+9*scale,y+h),index,effective_height=h))
    if kind=='large_title':
        lines.append(_metric_fixture_line('Independent heading',(left,40*scale,left+width*scale,58*scale),7,effective_height=18*scale))
    source=_PageSource((left+500*scale,400*scale),lines,[],[],image_bboxes=[] if kind=='no_raster' else [image])
    result=_detect_native_raster_axis_graphics(source)
    assert bool(result)==(kind in {'chart','large_title'})
    if result and kind=='large_title': assert result[0][1]>60*scale


@pytest.mark.parametrize('scale,left',[(.7,20),(1,60),(1.6,110)])
@pytest.mark.parametrize('white',[False,True])
@pytest.mark.parametrize('kind',['noise','dark','colored','unknown','same_font','no_prose','outside','superscript','normal_number_font','faint_body'])
def test_nested_digit_noise_needs_isolated_font_faint_paint_and_strong_body_contrast(scale,left,white,kind):
    """浅灰且异字体的短数字嵌入大段正文才是噪声；正文、脚注、图表数字、彩色和未知颜色均保留。"""
    from test_flash_review_20261003 import _metric_fixture_line
    from docvortex.analyzers.native.pdf.models import _PageSource
    from docvortex.analyzers.native.pdf.text_noise import exclude_faint_native_noise
    h=10*scale
    prose=[_metric_fixture_line('Natural explanatory text continues across this column.',
                               (left,(40+index*18)*scale,left+210*scale,(50+index*18)*scale),index,
                               effective_height=h,font_signature=('Prose',0),chars=[{'char':'A','char_idx':index}]) for index in range(3)]
    noise=_metric_fixture_line('7', (left+(230 if kind=='outside' else 70)*scale,63*scale,
                               left+(236 if kind=='outside' else 76)*scale,(66 if kind=='superscript' else 72)*scale),3,
                               effective_height=(3 if kind=='superscript' else 9)*scale,
                               font_signature=('Prose' if kind=='same_font' else 'Overlay',0),chars=[{'char':'7','char_idx':3}])
    lines=prose+[noise]
    if kind=='normal_number_font':
        lines.append(_metric_fixture_line('Normal explanatory label',(left,110*scale,left+150*scale,120*scale),4,
                                          effective_height=h,font_signature=('Overlay',0)))
    source=_PageSource((left+500*scale,300*scale),[noise] if kind=='no_prose' else lines,[c for line in lines for c in line.chars],[])
    def paint(indices):
        """提供独立绘制色证据，未知颜色不伪造为浅色。"""
        reference=(255,255,255,255) if white else (20,20,20,255)
        colors={index:(199,199,204,255) if kind=='faint_body' else reference for index in range(3)}
        if kind!='unknown': colors[3]=(20,20,20,255) if kind=='dark' else (190,170,230,255) if kind=='colored' else (199,199,204,255)
        return colors
    excluded=exclude_faint_native_noise(source,paint)
    assert excluded==({3} if kind=='noise' else set())
    assert (any(char.get('char_idx')==3 for char in source.chars))==(kind!='noise')


@pytest.mark.parametrize('scale,left',[(.7,20),(1,60),(1.6,110)])
@pytest.mark.parametrize('kind',['stage','missing_clip','no_stages','all_stages','bad_stage_order','drifting_clip','missing_column','overlapping_rows'])
def test_multiline_stage_grid_requires_repeated_native_cell_frames_and_consecutive_stage_groups(scale,left,kind):
    """改变位置字号，原生文字框与连续阶段编号共同证明跨行；普通行、缺栏和错位文字框不能猜合并。"""
    from test_flash_review_20261003 import _metric_fixture_line
    from docvortex.analyzers.native.pdf.models import _PageSource
    from docvortex.analyzers.native.pdf.table_detection import _native_clip_stage_grid
    from docvortex.document.pdf.native_contracts import PDFPathInfo
    h=10*scale;starts=[0,100,220,420];widths=[80,100,180,160];lines=[];paths=[]
    for column in range(4):
        x=left+starts[column]*scale
        lines.append(_metric_fixture_line('Other column label',(x,30*scale,x+70*scale,42*scale),len(lines),effective_height=12*scale))
        paths.append(PDFPathInfo((x,29*scale,x+widths[column]*scale,44*scale),5,False,False,0,0))
    labels=list(lines)
    for row in range(5):
        top=(60+row*30)*scale
        stage=row+1 if kind=='all_stages' else {0:1,1:2,3:4 if kind=='bad_stage_order' else 3}.get(row)
        for column in range(4):
            x=left+(starts[column]+(8 if kind=='drifting_clip' and column==1 and row==2 else 0))*scale
            if kind!='missing_clip' or column!=1 or row!=1:
                paths.append(PDFPathInfo((x,top,x+widths[column]*scale,top+24*scale),5,False,False,0,0))
            if column==0 and stage is None:continue
            if kind=='missing_column' and row==2 and column==3:continue
            text=f'{stage}. Natural group' if column==0 and kind!='no_stages' else 'Other independent label'
            lines.append(_metric_fixture_line(text,(left+starts[column]*scale,top+2*scale,left+(starts[column]+70)*scale,top+12*scale),len(lines),effective_height=h))
            if column:
                bottom=top+(33 if kind=='overlapping_rows' and row==1 else 24)*scale
                lines.append(_metric_fixture_line('Native wrapped cell',(left+starts[column]*scale,top+14*scale,left+(starts[column]+70)*scale,bottom),len(lines),effective_height=h))
    bounds=(left-5*scale,28*scale,left+590*scale,210*scale)
    source=_PageSource((left+800*scale,300*scale),lines,[],[],path_infos=paths)
    result=_native_clip_stage_grid(source,labels,bounds)
    assert bool(result)==(kind=='stage')
    if result:
        internal=[line for line in result if line.orientation=='horizontal' and line.bbox[0]>bounds[0]]
        assert len(internal)==2


@pytest.mark.parametrize('scale,left',[(.7,20),(1,60),(1.6,110)])
@pytest.mark.parametrize('kind',['notes','no_reference','no_chart','far','upper','bold','ordinary','font_boundary','wrong_sequence'])
def test_numbered_chart_notes_need_native_reference_nearby_plot_and_stable_continuation(scale,left,kind):
    """无线图下说明须由原生上标和图体净空证明，连续编号分项；普通正文、远距、粗体和样式边界保持独立。"""
    from test_flash_review_20261003 import _metric_fixture_line
    from docvortex.analyzers.native.pdf.auxiliary_text import _chart_referenced_note_groups
    h=6*scale;top=(100 if kind=='upper' else 285 if kind=='far' else 220)*scale
    rows=[_metric_fixture_line('Independent normal explanation.' if kind=='ordinary' else '1 Natural note about the measurements.',
                              (left,top,left+200*scale,top+h),0,effective_height=h,font_signature=('Note',0),dominant_font_weight=700 if kind=='bold' else 400),
          _metric_fixture_line('The original continuation remains complete.',(left,top+9*scale,left+190*scale,top+15*scale),1,
                               effective_height=h,font_signature=('Other' if kind=='font_boundary' else 'Note',0)),
          _metric_fixture_line(('4' if kind=='wrong_sequence' else '2')+' Another independent note.',
                               (left,top+18*scale,left+180*scale,top+24*scale),2,effective_height=h,font_signature=('Note',0))]
    plot=(left-5*scale,60*scale,left+210*scale,190*scale)
    result=_chart_referenced_note_groups([(line,line.bbox) for line in rows],[] if kind=='no_chart' else [plot],
                                        set() if kind=='no_reference' else {'1'},(500*scale,300*scale))
    assert result==([{0,1},{2}] if kind=='notes' else [{0}] if kind=='font_boundary' else [{0,1}] if kind=='wrong_sequence' else [])


@pytest.mark.parametrize('scale,left,width',[(.7,20,160),(1,60,210),(1.6,110,280)])
@pytest.mark.parametrize('kind',['wrap','terminal','font','far','list','caption','no_plot'])
def test_short_native_description_tail_above_proven_raster_chart_joins_only_its_own_matching_long_row(scale,left,width,kind):
    """图上方说明尾行须同式同栏紧贴；句末、异式、远距、列表和图注不能因短行被拼入说明。"""
    from test_flash_review_20261003 import _metric_fixture_line
    from docvortex.analyzers.native.pdf.models import _PageSource
    from docvortex.analyzers.native.pdf.graphics import group_native_raster_chart_descriptions
    h=10*scale;lines=[]
    first=_metric_fixture_line('Natural descriptive heading'+('.' if kind=='terminal' else ''),(left,30*scale,left+width*scale,40*scale),0,
                               effective_height=h,font_signature=('Description',0),paragraph_terminal=kind=='terminal')
    tail=_metric_fixture_line('2. Native entry' if kind=='list' else 'continued description',(left,(65 if kind=='far' else 46)*scale,left+50*scale,(75 if kind=='far' else 56)*scale),1,
                              effective_height=h,font_signature=('Other' if kind=='font' else 'Description',0),caption_start=kind=='caption')
    lines=[first,tail]
    for index in range(7):
        y=(72+19*index)*scale
        lines.append(_metric_fixture_line(str(10+5*index),(left-12*scale,y,left-3*scale,y+6*scale),index+2,effective_height=6*scale))
    source=_PageSource((left+500*scale,300*scale),lines,[],[],image_bboxes=[] if kind=='no_plot' else [(left,70*scale,left+width*scale,190*scale)])
    group_native_raster_chart_descriptions(source)
    assert (first.paragraph_group is not None and first.paragraph_group==tail.paragraph_group)==(kind=='wrap')


@pytest.mark.parametrize('scale,left',[(.7,20),(1,60),(1.6,110)])
@pytest.mark.parametrize('kind',['gutter','dark','unknown','inside','no_plots','one_plot','normal_text'])
def test_empty_plot_gutter_noise_is_independent_of_axis_font_reuse_and_keeps_real_plot_values(scale,left,kind):
    """图间浅灰独立数字可复用正常刻度字体；图内刻度、深色数字、未知颜色和正常文字不排除。"""
    from test_flash_review_20261003 import _metric_fixture_line
    from docvortex.analyzers.native.pdf.models import _PageSource
    from docvortex.analyzers.native.pdf.text_noise import exclude_faint_native_noise
    h=6*scale;rows=[]
    for index in range(7):
        rows.append(_metric_fixture_line(str(10+index*5),(left, (60+index*18)*scale,left+9*scale,(66+index*18)*scale),index,
                                         effective_height=h,font_signature=('Numbers',0),chars=[{'char':'1','char_idx':index}]))
    for index in range(3):
        rows.append(_metric_fixture_line('Natural explanatory report text with complete meaning.',
                                         (left,220*scale+index*9*scale,left+220*scale,226*scale+index*9*scale),len(rows),
                                         effective_height=h,font_signature=('Body',0),chars=[{'char':'A','char_idx':len(rows)}]))
    x=left+(50 if kind=='inside' else 250)*scale
    noise=_metric_fixture_line('Native label' if kind=='normal_text' else '8',(x,100*scale,x+8*scale,106*scale),10,
                               effective_height=h,font_signature=('Numbers',0),chars=[{'char':'8','char_idx':10}])
    rows.append(noise)
    plots=[(left-5*scale,50*scale,left+200*scale,190*scale),(left+300*scale,50*scale,left+490*scale,190*scale)]
    if kind=='no_plots':plots=[]
    if kind=='one_plot':plots=plots[:1]
    source=_PageSource((left+600*scale,300*scale),rows,[char for row in rows for char in row.chars],[])
    def paint(indices):
        """保留正常数字黑色，噪声颜色缺失时不能猜测。"""
        colors={index:(20,20,20,255) for index in range(10)}
        if kind!='unknown':colors[10]=(20,20,20,255) if kind=='dark' else (199,199,204,255)
        return colors
    assert exclude_faint_native_noise(source,paint,plots)==({10} if kind=='gutter' else set())
    assert all(any(line.source_index==index for line in source.lines) for index in range(7))
