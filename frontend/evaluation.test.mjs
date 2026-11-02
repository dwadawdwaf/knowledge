import test from 'node:test';
import assert from 'node:assert/strict';
import { parseReport, needsReview, VARIANTS, REPORT_FILES } from './evaluation.mjs';

function row() {
  return { id: 'E01', category: '自制', question: '<script>not executable</script>', answerable: true,
    retrieval: Object.fromEntries(VARIANTS.map(([key]) => [key, { fact_recall: 1 }])),
    qa: { status_matches_label: true, lexical_coverage: 1, citation_fact_coverage: 1, citations_resolvable: true } };
}
function report() {
  return { schema: 'retrieval-evaluation-v1', status: 'completed', cases: [row()], events: [], sources: [], knowledge_base_id: 'synthetic', total_cases: 24 };
}
test('读取完整评测报告，正文仍为数据', () => {
  assert.equal(parseReport(JSON.stringify(report())).cases[0].question, '<script>not executable</script>');
});
test('损坏、未知版本和缺失方案不展示伪指标', () => {
  for (const value of [null, {}, { ...report(), status: 'fake' }, { ...report(), cases: [{}] }]) {
    assert.throws(() => parseReport(JSON.stringify(value)));
  }
  assert.throws(() => parseReport('x'.repeat(1024 * 1024 + 1)));
});
test('召回缺失、错误、降级和拒答错误均需复核', () => {
  const valid = row();
  assert.equal(needsReview(valid), false);
  for (const change of [
    value => { value.qa.error = 'MODEL_UNAVAILABLE'; },
    value => { value.qa.status_matches_label = false; },
    value => { value.qa.forbidden_detected = true; },
    value => { value.qa.citations_resolvable = false; },
    value => { value.retrieval.hybrid_5.fact_recall = 0.5; },
    value => { value.retrieval.keyword_5.degraded = true; },
  ]) {
    const value = structuredClone(valid); change(value); assert.equal(needsReview(value), true);
  }
});
test('正常无答案不按缺失事实指标判失败', () => {
  const value = row(); value.answerable = false;
  value.qa.lexical_coverage = null; value.qa.citation_fact_coverage = null;
  value.retrieval.hybrid_5.fact_recall = null;
  assert.equal(needsReview(value), false);
});

test('兼容两轮报告且严格限制48题', () => {
  assert.equal(REPORT_FILES.length, 2);
  const value = { ...report(), total_cases: 48, cases: Array.from({ length: 48 }, (_, i) => ({ ...row(), id: String(i), partition: i < 24 ? 'development' : 'holdout' })) };
  assert.equal(parseReport(JSON.stringify(value)).cases.length, 48);
  assert.throws(() => parseReport(JSON.stringify({ ...value, total_cases: 49 })));
  assert.throws(() => parseReport(JSON.stringify({ ...value, cases: [...value.cases, row()] })));
  assert.throws(() => parseReport(JSON.stringify({ ...value, total_cases: 24 })));
});
