"""内部自有候选流适配；文字判定保留 Python，数值展开与动态合并连续执行。"""


class _NativeCoreLineSet:
    """以 Rust 核心成员和少量新增注释行提供只读查询及局部选择集合。"""

    def __init__(self, native, extra=None):
        """不枚举核心成员，只保存当前注释扫描新增的少量行号。"""
        self.native = native
        self.extra = set() if extra is None else extra

    def __len__(self):
        """不导出核心集合即可判断大小和真假值。"""
        return len(self.native) + len(self.extra)

    def __iter__(self):
        """仅在显式枚举边界物化成员，普通包含查询保持核心集合驻留 Rust。"""
        yield from self.native.members()
        yield from self.extra

    def __contains__(self, value):
        """注释通常只查询少量来源行，保持核心集合驻留 Rust。"""
        return value in self.extra or self.native.contains_all([value])

    def copy(self):
        """注释扫描拥有独立新增集合，不修改候选核心。"""
        return _NativeCoreLineSet(self.native, self.extra.copy())

    def issuperset(self, values):
        """一次查询整条注释行的来源成员，避免 Python 集合转换整个核心。"""
        return self.native.contains_all([value for value in values if value not in self.extra])

    def update(self, values):
        """仅记录核心之外新选中的注释成员。"""
        self.extra.update(self.native.difference(list(values)))

    def intersection(self, values):
        """仅遍历较小的注释集合，供已有注释几何缓存复用。"""
        values = list(values)
        result = set(self.native.intersection(values))
        if self.extra:
            result.update(self.extra.intersection(values))
        return result


def _annotations(values):
    """保留注释类型、成员与行框字典插入顺序，不重新裁决注释文字。"""
    return [(a.kind, a.bbox, list(a.line_indices), list(a.line_bboxes.items())) for a in values]


def merge_owned(candidates):
    """统一使用原生候选合并，Python 保留语义扩展与任意整数分数的稳定排序。"""
    from ...._compute_backend import get_native
    from . import table_rules as rules
    from .models import _TableAnnotation, _TableCandidate

    native = get_native()
    ordered = sorted(candidates, key=lambda item: item.score, reverse=True)
    grids = {}
    scores = {}
    merger = native.NativeTableMerger()
    for position, candidate in enumerate(ordered):
        # 原始分数只参与 Python 的一次稳定排序；有界名次穿过 FFI，避免大整数分数被舍入或溢出。
        rank = float(len(ordered) - position)
        scores[rank] = candidate.score
        if isinstance(candidate, rules._RuleCandidateDraft):
            prepared = candidate.prepared_candidate
            if prepared is None or not isinstance(prepared[0].native, native.PreparedRuleCandidates):
                candidate = candidate.materialize()
            else:
                context = candidate.context
                if id(context) not in grids:
                    if context.grids is None:
                        context.grids = [
                            box
                            for box in rules._connected_rule_grid_bboxes(
                                context.axis_lines,
                                context.median_height,
                                components=context.grid_components,
                            )
                            if not any(
                                rules._bbox_overlap_in_smaller(box, excluded) >= 0.5 for excluded in context.excluded_bboxes
                            )
                        ]
                    rows = [
                        (row.center_y, [(fragment.line_index, fragment.local_bbox) for fragment in row.fragments])
                        for row in context.rows
                    ]
                    grids[id(context)] = native.NativeTableGrid(
                        context.grids,
                        rows,
                        context.page_size,
                        context.angle,
                        context.median_height,
                    )
                if context.annotation_geometry is None:
                    context.annotation_geometry = rules._prepare_annotation_geometry(context.rows) or False
                bbox, local, core, members, annotations = rules._expand_rule_table_candidate(
                    candidate.boundaries,
                    candidate.rows,
                    context.rows,
                    context.lines,
                    context.page_size,
                    context.angle,
                    context.median_height,
                    candidate.caption,
                    candidate.has_notes,
                    context.marker_cache,
                    candidate.note_rows,
                    context.body_metrics,
                    candidate.core_query,
                    context.annotation_geometry or None,
                    prepared,
                    owned=True,
                )
                merger.add(
                    bbox, local, context.angle, rank, core, [], _annotations(annotations), members.native, grids[id(context)]
                )
                continue
        merger.add(
            candidate.bbox,
            candidate.local_bbox,
            candidate.angle,
            rank,
            candidate.core_bbox,
            list(candidate.line_indices),
            _annotations(candidate.annotations),
        )
    return [
        _TableCandidate(
            tuple(bbox),
            tuple(local),
            angle,
            scores[rank],
            tuple(core) if core is not None else None,
            set(members),
            [
                _TableAnnotation(kind, tuple(box), set(ids), {key: tuple(value) for key, value in rows})
                for kind, box, ids, rows in annotations
            ],
        )
        for bbox, local, angle, rank, core, members, annotations in merger.finish()
    ]
