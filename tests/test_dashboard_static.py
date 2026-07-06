from pathlib import Path


def test_dashboard_comments_show_author_and_time_without_emotion_fields():
    html = Path("dashboard.html").read_text(encoding="utf-8")

    assert "var commentTime = c.original_feel_created || c.created || '';" in html
    assert "let dashboardAiAuthor = 'Haven';" in html
    assert "function commentAuthorName(comment)" in html
    assert '<div class="comment-meta">' in html
    assert '<span class="comment-author">' in html
    assert '<span class="comment-time">' in html

    comments_block = html.split("var commentsHtml = comments.length", 1)[1].split("var commentFormHtml", 1)[0]
    assert "commentAuthorName(c)" in comments_block
    assert "c.valence" not in comments_block
    assert "c.arousal" not in comments_block


def test_dashboard_comment_enter_submit_has_no_visible_send_key():
    html = Path("dashboard.html").read_text(encoding="utf-8")
    form_block = html.split("var commentFormHtml =", 1)[1].split("content.innerHTML =", 1)[0]

    assert "handleCommentKeydown(event)" in form_block
    assert "comment-send-button" not in html
    assert 'aria-label="发送"' not in form_block


def test_dashboard_bucket_detail_loads_moment_diagnostics():
    html = Path("dashboard.html").read_text(encoding="utf-8")
    detail_block = html.split("async function showDetail", 1)[1].split("function startBucketContentEdit", 1)[0]

    assert 'id="bucket-moments-block"' in detail_block
    assert "loadBucketMoments(id);" in detail_block
    assert "BASE + '/api/moments?bucket_id='" in html
    assert "function renderBucketMoments(data)" in html
    assert "function renderMomentItem(moment, index)" in html
    assert "function renderMomentEdges(edges)" in html
    assert "function renderMomentEdge(edge)" in html
    assert "Moment Edges" in html
    assert "moment-source-window" in html
    assert "原文窗口" in html
    assert "m.source_window" in html
    assert "runtime_gate" in html
    assert ".detail-moments" in html
    assert ".moment-edge-list" in html


def test_dashboard_bucket_detail_can_edit_event_date_separately():
    html = Path("dashboard.html").read_text(encoding="utf-8")
    detail_block = html.split("async function showDetail", 1)[1].split("function loadBucketMoments", 1)[0]
    date_submit = html.split("async function submitBucketDateEdit", 1)[1].split("async function submitBucketTitleEdit", 1)[0]

    assert 'id="bucket-date-view"' in detail_block
    assert 'id="bucket-date-edit"' in detail_block
    assert 'name="date" type="date"' in detail_block
    assert "startBucketDateEdit()" in detail_block
    assert "function dateInputValue(value)" in html
    assert "JSON.stringify({date: newDate})" in date_submit
    assert "await loadBuckets();" in date_submit
    assert "JSON.stringify({name:" not in date_submit
    assert "JSON.stringify({content:" not in date_submit


def test_dashboard_bucket_list_has_bulk_delete_controls():
    html = Path("dashboard.html").read_text(encoding="utf-8")
    list_view = html.split('id="list-view"', 1)[1].split('id="breath-view"', 1)[0]

    assert 'id="bucket-bulk-toolbar"' in list_view
    assert "toggleBucketBulkMode()" in list_view
    assert "selectCurrentBuckets()" in list_view
    assert "deleteSelectedBuckets()" in list_view
    assert "BASE + '/api/buckets/delete'" in html
    assert "confirm: 'DELETE'" in html
    assert "bucketBulkDeleteBlockReason" in html
    assert "受保护记忆不能批量删除" in html


def test_dashboard_feel_filter_excludes_daily_impressions():
    html = Path("dashboard.html").read_text(encoding="utf-8")

    assert "{ key: 'daily_impression', label: '日印象' }" in html
    assert "currentFilter === 'feel'" in html
    assert "b.type === 'feel' && !isDailyImpressionBucket(b)" in html
    assert "currentFilter === 'daily_impression'" in html
    assert "buckets.filter(isDailyImpressionBucket)" in html


def test_dashboard_reflection_events_use_created_fallback_and_filter_profile_sources():
    html = Path("dashboard.html").read_text(encoding="utf-8")
    event_date_block = html.split("function getBucketEventDate", 1)[1].split("function isDailyImpressionBucket", 1)[0]
    source_block = html.split("function reflectionSourceChips", 1)[1].split("async function hydrateReflectionEntryContents", 1)[0]

    assert "if (bucket.created) return String(bucket.created).slice(0, 10);" in event_date_block
    assert "function isProfileFactBucket" in html
    assert "isProfileFactBucket(bucket)" in html
    assert "function isReflectionSourceBucket" in html
    assert "sourceDate === reflectionDate" in html
    assert "日印象参考记忆桶" in source_block
    assert "reflectionSourceChips(detail, bucket.reflection_date)" in html


def test_dashboard_exposes_darkroom_door_without_release_or_body_fields():
    html = Path("dashboard.html").read_text(encoding="utf-8")
    door_block = html.split('id="darkroom-door"', 1)[1].split('<div class="search-bar">', 1)[0]
    render_block = html.split("function renderDarkroomDoor", 1)[1].split("async function loadDarkroomDoor", 1)[0]
    load_block = html.split("async function loadDarkroomDoor", 1)[1].split("function monthStart", 1)[0]

    assert "Darkroom Door" in door_block
    assert "BASE + '/api/darkroom/status'" in load_block
    assert "loadDarkroomDoor();" in html
    assert "previous_completeness" in render_block
    assert "last_completeness" in render_block
    assert "darkroom_release" not in html
    assert ".content" not in render_block
    assert ".note" not in render_block


def test_dashboard_breath_debug_loads_diffusion_paths():
    html = Path("dashboard.html").read_text(encoding="utf-8")
    breath_block = html.split("async function runBreathDebug()", 1)[1].split("function breathGateTrace", 1)[0]

    assert 'id="diffusion-results"' in html
    assert "loadDiffusionDebug(query);" in breath_block
    assert "BASE + '/api/diffusion-debug?q='" in html
    assert "function renderDiffusionDebug(data)" in html
    assert "function renderDiffusionRow(item, index, kind)" in html
    assert ".diffusion-panel" in html


def test_dashboard_breath_debug_loads_recall_moment_candidates():
    html = Path("dashboard.html").read_text(encoding="utf-8")
    breath_block = html.split("async function runBreathDebug()", 1)[1].split("async function loadDiffusionDebug", 1)[0]

    assert 'id="recall-results"' in html
    assert 'id="breath-rerank"' in html
    assert "if (useRerank) url += '&rerank=1';" in breath_block
    assert "renderRecallDebug(data.rerank)" in breath_block
    assert "loadRecallDebug(query);" in breath_block
    assert "BASE + '/api/recall-debug?q='" in html
    assert "function renderRecallDebug(data)" in html
    assert "function renderRecallCandidate(candidate)" in html
    assert "selected_secondary" in html
    assert "direct_render.shape" in html
    assert "render ' + c.direct_render.shape" in html
    assert "Moment 命中" in html


def test_dashboard_breath_view_loads_gateway_injection_debug():
    html = Path("dashboard.html").read_text(encoding="utf-8")

    assert 'id="gateway-injections-panel"' in html
    assert 'id="gateway-session-filter"' in html
    assert "loadGatewayInjections()" in html
    assert "BASE + '/api/gateway-injections?limit=10" in html
    assert "function renderGatewayInjections(data)" in html
    assert "function renderGatewayInjectionItem(item)" in html
    assert "function gatewayDirectRenderSummaries(payload)" in html
    assert "function gatewayChainDebugSummaries(payload)" in html
    assert "direct render:" in html
    assert "diffused chain:" in html
    assert "date trace yes" in html
    assert "date trace no" in html
    assert 'class="gateway-injections-content"' in html
    assert 'class="gateway-injection-list"' in html
    assert 'class="gateway-injection-item"' in html
    assert '.gateway-injection-controls input[type="text"]' in html
    assert '.gateway-injection-controls input[type="checkbox"]' in html
    assert "gateway-injection-empty" in html
    assert "row.direct_render" in html
    assert "diffused_moment_debug" in html
    assert "Gateway 最近注入" in html


def test_dashboard_exposes_profile_fact_page():
    html = Path("dashboard.html").read_text(encoding="utf-8")

    assert 'data-tab="profile"' in html
    assert 'id="profile-view"' in html
    assert 'id="profile-facts-list"' in html
    assert 'id="profile-summary"' in html
    assert "loadProfileFacts()" in html
    assert "renderProfileFactCard" in html
    assert "runProfileFactAction" in html
    assert "editProfileFact" in html
    assert "generateProfileFactProposals" in html
    assert "confirmProfileFactProposal" in html
    assert "generateAnchorProposals" in html
    assert "confirmAnchorProposal" in html
    assert "BASE + '/api/profile-facts'" in html
    assert "BASE + '/api/profile-facts/'" in html
    assert "BASE + '/api/profile-fact-proposals'" in html
    assert "BASE + '/api/profile-fact-proposals/confirm'" in html
    assert "BASE + '/api/anchor-proposals'" in html
    assert "BASE + '/api/anchor-proposals/confirm'" in html
    assert 'id="profile-proposal-bucket-id"' in html
    assert 'id="profile-proposal-list"' in html
    assert 'id="anchor-proposal-bucket-id"' in html
    assert 'id="anchor-proposal-list"' in html
    assert "打开证据" in html
    assert "生成画像候选" in html
    assert "生成 Anchor 候选" in html
    assert "确认写入" in html
    assert "确认标为 Anchor" in html
    assert "Profile Facts" in html
    assert ".profile-card" in html
    assert ".profile-proposal-panel" in html


def test_dashboard_exposes_portrait_state_panel():
    html = Path("dashboard.html").read_text(encoding="utf-8")
    profile_tab_block = html.split("if (target === 'profile')", 1)[1].split("if (target === 'word-map')", 1)[0]
    load_buckets_block = html.split("async function loadBuckets", 1)[1].split("function getActiveTab()", 1)[0]

    assert 'id="portrait-state-panel"' in html
    assert 'id="portrait-state-summary"' in html
    assert 'id="portrait-state-content"' in html
    assert 'id="portrait-maintain-message"' in html
    assert "Portrait State" in html
    assert "只读，不写 profile_fact、anchor 或 Core Memory" in html
    assert "loadPortraitState()" in html
    assert "refreshPortraitView()" in html
    assert "runPortraitMaintain" in html
    assert "resetPortraitState" in html
    assert "Recent Timeline" in html
    assert "renderPortraitSelfAnchor" in html
    assert "自我总入口" in html
    assert "loadBuckets({ skipProfileRefresh: true });" in html
    assert "var deleteSpec = item._delete ? escAttr(jsString(JSON.stringify(item._delete))) : '';" in html
    assert "renderPortraitState" in html
    assert "renderPortraitScope" in html
    assert "renderPortraitCandidates" in html
    assert "renderPortraitEvidence" in html
    assert "BASE + '/api/portrait-state'" in html
    assert "BASE + '/api/portrait-maintain'" in html
    assert "BASE + '/api/portrait-state/reset'" in html
    assert "body: JSON.stringify({ force: true })" in html
    assert "body: JSON.stringify({ confirm: 'RESET' })" in html
    assert "read only" in html
    assert "手动生成" in html
    assert "清空画像" in html
    assert "state.recent_timeline" in html
    assert "state.stable_candidates" in html
    assert "state.profile_fact_candidates" in html
    assert "state.self_anchor_entry" in html
    assert "renderPortraitSelfAnchor(state.self_anchor_entry)" in html
    assert "cfg-self-anchor-entry" in html
    assert "renderPortraitScope('persona'" not in html
    assert ".portrait-state-grid" in html
    assert ".portrait-candidate-grid" in html
    assert "loadPortraitState();" in profile_tab_block
    assert "loadProfileFacts();" in profile_tab_block
    assert "loadPortraitState();" in load_buckets_block


def test_dashboard_exposes_todo_page():
    html = Path("dashboard.html").read_text(encoding="utf-8")
    tab_block = html.split("document.querySelectorAll('.tab')", 1)[1].split("let searchTimer", 1)[0]
    todo_block = html.split("async function loadTodos()", 1)[1].split("function formatDarkroomCompleteness", 1)[0]

    assert 'data-tab="todos"' in html
    assert 'id="todo-view"' in html
    assert 'id="todo-list"' in html
    assert 'id="todo-status"' in html
    assert 'id="todo-editor"' in html
    assert "照顾备忘" in html
    assert "openTodoCreateForm()" in html
    assert "openTodoEditForm" in html
    assert "saveTodoForm" in html
    assert "setTodoStatusFilter('active')" in html
    assert "setTodoStatusFilter('done')" in html
    assert "setTodoStatusFilter('archived')" in html
    assert "setTodoStatusFilter('all')" in html
    assert "loadTodos()" in tab_block
    assert "BASE + '/api/reminders?status='" in todo_block
    assert "BASE + '/api/reminders/' + encodeURIComponent(id)" in html
    assert "标完成" in html
    assert "不从记忆桶派生" in html
    assert "不会触发 embedding 重建" in html
    assert "每天最多" in html
    assert "今日已注入" not in html
    assert "总注入" not in html
    assert "写回桶" not in html
    assert "writebackTodo" not in html


def test_dashboard_keeps_compact_legacy_filter_row_and_compatible_filters():
    html = Path("dashboard.html").read_text(encoding="utf-8")
    stats_block = html.split("function updateStats()", 1)[1].split("function buildFilters()", 1)[0]
    build_block = html.split("function buildFilters()", 1)[1].split("function filterBuckets", 1)[0]
    filter_block = html.split("function filterBuckets", 1)[1].split("function bucketBulkDeleteBlockReason", 1)[0]

    assert "function isSelfAnchorBucket" in html
    assert "tags.includes('self_identity')" not in html
    assert "tag:self_anchor" in build_block
    assert "label: '自我'" in build_block
    assert "label: '全部'" in build_block
    assert "label: '📌 钉选'" in build_block
    assert "label: '⚓ Anchor'" in build_block
    assert "label: 'Feel'" in build_block
    assert "label: '日印象'" in build_block
    assert "label: '⚡ 可浮现'" in build_block
    assert "未解决" not in build_block
    assert "旧视图" not in build_block
    assert "label: '🌿 已消化'" in build_block
    assert "label: '📦 归档'" in build_block
    assert "已沉底" in stats_block
    assert "不浮现" not in stats_block
    assert "const archived = allBuckets.filter(b => b.type === 'archived').length;" in stats_block
    assert " + archived + ' 归档'" in stats_block
    assert "旧标签 / legacy domain" not in build_block
    assert "canonicalDomains" not in build_block
    assert "kindFilters" not in build_block
    assert "statusFilters" not in build_block
    assert "flagFilters" not in build_block
    assert "domainFilters" not in build_block
    assert "domainOptions.map" not in build_block
    assert "key: 'profile'" not in build_block
    assert "label: '画像'" not in build_block
    assert "filters.onclick = function(e)" in build_block
    assert "filters.addEventListener" not in build_block
    assert "function isSurfaceableBucket(bucket)" in html
    assert "currentFilter === 'profile'" not in filter_block
    assert "currentFilter === 'tag:self_anchor'" in filter_block
    assert "currentFilter === 'unresolved') return buckets.filter(isSurfaceableBucket)" in filter_block
    assert "type === 'archived'" in html
    assert "bucket.digested || meta.digested" in html
    assert "isSurfaceableBucket(b) ? '✓' : '—'" in html
    assert "currentFilter.startsWith('canonical_domain:')" in filter_block
    assert "currentFilter.startsWith('legacy_domain:')" in filter_block


def test_dashboard_bucket_sort_toggle_uses_created_time_or_weight():
    html = Path("dashboard.html").read_text(encoding="utf-8")
    sort_block = html.split("function bucketCreatedTime", 1)[1].split("function bucketBulkDeleteBlockReason", 1)[0]
    render_block = html.split("function renderBuckets", 1)[1].split("async function searchBuckets", 1)[0]

    assert 'id="bucket-sort-toggle"' in html
    assert "let bucketSortMode = 'time';" in html
    assert "let bucketSortDescending = true;" in html
    assert "return Number.isFinite(parsed) ? parsed : 0;" in sort_block
    assert "bucketSortMode === 'weight' ? bucketWeightScore" in sort_block
    assert "bucketSortMode = bucketSortMode === 'time' ? 'weight' : 'time';" in sort_block
    assert "var sortedBuckets = sortBucketsForDisplay(buckets);" in render_block
    assert "formatTimeAgo(b.created || b.last_active)" in render_block


def test_dashboard_hides_confirm_button_for_active_profile_facts():
    html = Path("dashboard.html").read_text(encoding="utf-8")

    assert "var confirmButton = status.cls === 'active'" in html
    assert "? ''" in html
    assert "confirmButton +\n        '<button type=\"button\" onclick=\"editProfileFact" in html
    assert "runProfileFactAction(\\'' + jsString(id) + '\\', \\'confirm\\')" in html


def test_dashboard_keeps_proposal_confirm_success_messages():
    html = Path("dashboard.html").read_text(encoding="utf-8")
    profile_block = html.split("async function confirmProfileFactProposal", 1)[1].split("function setAnchorProposalMessage", 1)[0]
    anchor_block = html.split("async function confirmAnchorProposal", 1)[1].split("function renderProfileFacts", 1)[0]

    assert "var successMessage = '已写入画像事实 ' + (data.id || '');" in profile_block
    assert profile_block.index("renderProfileFactProposals({ proposals: profileFactProposals, rejected: [] });") < profile_block.index("setProfileProposalMessage(successMessage, 'ok');")
    assert "var successMessage = data.status === 'already_anchor' ? '已经是 Anchor。' : '已标为 Anchor ' + (data.id || '');" in anchor_block
    assert anchor_block.index("renderAnchorProposals({ proposals: anchorProposals, rejected: [] });") < anchor_block.index("setAnchorProposalMessage(successMessage, 'ok');")


def test_dashboard_exposes_word_map_page():
    html = Path("dashboard.html").read_text(encoding="utf-8")

    assert 'data-tab="word-map"' in html
    assert 'id="word-map-view"' in html
    assert 'id="word-map-summary"' in html
    assert 'id="word-map-nodes"' in html
    assert 'id="word-map-edges"' in html
    assert 'id="identity-aliases"' in html
    assert 'id="word-map-boundary"' in html
    assert "loadWordMap()" in html
    assert "rebuildWordMap()" in html
    assert "rebuildIdentitySemantics()" in html
    assert "BASE + '/api/word-map?nodes=20&edges=20'" in html
    assert "BASE + '/api/word-map/rebuild'" in html
    assert "BASE + '/api/word-map/cards" not in html
    assert "BASE + '/api/identity-semantics?limit=50'" in html
    assert "BASE + '/api/identity-semantics/rebuild'" in html
    assert ".word-map-card" in html
    assert "不会自动注入 Gateway" in html
    assert "未配置私有 canonical" in html


def test_dashboard_exposes_gateway_memory_cooldown_settings():
    html = Path("dashboard.html").read_text(encoding="utf-8")
    load_block = html.split("async function loadConfig()", 1)[1].split("async function saveConfig", 1)[0]
    save_block = html.split("async function saveConfig", 1)[1].split("var keyVal =", 1)[0]
    config_view = html.split('id="config-view"', 1)[1].split('id="detail-panel"', 1)[0]

    assert 'data-tab="memory-config"' in html
    assert html.index('data-tab="config"') < html.index('data-tab="memory-config"')
    assert html.index('data-tab="config"') < html.index('data-tab="upstream-config"')
    assert html.index('data-tab="upstream-config"') < html.index('data-tab="memory-config"')
    assert html.index('data-tab="memory-config"') < html.index('data-tab="import"')
    assert 'id="memory-config-view"' in html
    assert 'id="memory-config-status"' in html
    assert "<h3>记忆浮现</h3>" in html
    assert 'id="cfg-recent-context-enabled"' in html
    assert 'id="cfg-persona-context-enabled"' in html
    assert 'id="cfg-persona-context-rounds"' in html
    assert 'id="cfg-gateway-cooldown"' in html
    assert 'id="cfg-gateway-rounds"' in html
    assert 'id="cfg-direct-render-mode"' in html
    assert 'id="cfg-retrieval-mode"' in html
    assert 'id="cfg-operit-context-rewrite-enabled"' in html
    assert 'id="cfg-word-map-hint-enabled"' in html
    assert 'id="cfg-query-planner-enabled"' in html
    assert 'id="cfg-memory-sentinel-llm-enabled"' not in html
    assert 'id="cfg-domain-sentinel-enabled"' in html
    assert 'id="cfg-domain-sentinel-model"' in html
    assert 'id="cfg-domain-sentinel-url"' in html
    assert 'id="cfg-domain-sentinel-key"' in html
    assert 'id="cfg-domain-sentinel-thinking"' not in html
    assert 'id="cfg-query-planner-model"' not in html
    assert 'id="cfg-memory-detail-recall-enabled"' in html
    assert 'id="cfg-memory-detail-recall-max-ids"' in html
    assert 'id="cfg-memory-detail-recall-budget"' in html
    assert "Targeted Memory Detail 不依赖这个开关" in html
    assert 'id="cfg-diffusion-enabled"' in html
    assert 'id="cfg-diffusion-topk"' in html
    assert 'id="cfg-diffusion-min"' in html
    assert 'id="cfg-chain-walk"' in html
    assert 'id="cfg-chain-hops"' in html
    assert 'id="cfg-chain-confidence"' in html
    assert 'id="cfg-chain-frontier"' in html
    assert "breath 和 Gateway 都会立即读取新的扩散参数" in html
    assert "Gateway 需要重启" not in html
    assert "cfg.gateway.cooldown_hours" in html
    assert "cfg.gateway.skip_recent_rounds" in html
    assert "cfg.gateway.current_inner_state_interval_rounds" in html
    assert "cfg.gateway.direct_render_mode" in html
    assert "cfg.gateway.retrieval_mode" in html
    assert "cfg.gateway.operit_context_rewrite_enabled" in html
    assert "cfg.gateway.word_map_hint_enabled" in html
    assert "cfg.gateway.query_planner_enabled" in html
    assert "cfg.gateway.memory_sentinel_llm_enabled" not in html
    assert "cfg.gateway.domain_sentinel_enabled" in html
    assert "cfg.gateway.domain_sentinel_model" in html
    assert "cfg.gateway.domain_sentinel_base_url" in html
    assert "cfg.gateway.domain_sentinel_api_key_masked" in html
    assert "推荐用便宜快速的模型" in html
    assert "cfg.gateway.domain_sentinel_enable_thinking" not in html
    assert "cfg.gateway.query_planner_model" not in html
    assert "cfg.gateway.memory_detail_recall_enabled" in html
    assert "cfg.gateway.memory_detail_recall_max_ids" in html
    assert "cfg.gateway.memory_detail_recall_budget" in html
    assert "((cfg.gateway && cfg.gateway.recent_context_budget) ?? 300) > 0 ? 'true' : 'false'" in load_block
    assert "personaRounds > 0 ? 'true' : 'false'" in load_block
    assert "setConfigStatus" in html
    assert 'id="cfg-gateway-cooldown"' not in config_view
    assert "cfg.memory_diffusion || {}" in load_block
    assert "diffusion.chain_walk_enabled" in load_block
    assert "diffusion.chain_min_confidence" in load_block
    assert "cooldown_hours: floatValue('cfg-gateway-cooldown', 6)" in html
    assert "skip_recent_rounds: numberValue('cfg-gateway-rounds', 5)" in html
    assert "current_inner_state_interval_rounds: personaContextRounds," in html
    assert "direct_render_mode: document.getElementById('cfg-direct-render-mode').value," in html
    assert "retrieval_mode: document.getElementById('cfg-retrieval-mode').value," in html
    assert "operit_context_rewrite_enabled: document.getElementById('cfg-operit-context-rewrite-enabled').value === 'true'," in html
    assert "word_map_hint_enabled: document.getElementById('cfg-word-map-hint-enabled').value === 'true'," in html
    assert "query_planner_enabled: document.getElementById('cfg-query-planner-enabled').value === 'true'," in html
    assert "memory_sentinel_llm_enabled:" not in html
    assert "domain_sentinel_enabled: document.getElementById('cfg-domain-sentinel-enabled').value === 'true'," in html
    assert "domain_sentinel_model: document.getElementById('cfg-domain-sentinel-model').value," in html
    assert "domain_sentinel_base_url: document.getElementById('cfg-domain-sentinel-url').value," in html
    assert "query_planner_model: document.getElementById('cfg-query-planner-model').value," not in html
    assert "memory_detail_recall_enabled: document.getElementById('cfg-memory-detail-recall-enabled').value === 'true'," in html
    assert "memory_detail_recall_max_ids: numberValue('cfg-memory-detail-recall-max-ids', 3)," in html
    assert "memory_detail_recall_budget: numberValue('cfg-memory-detail-recall-budget', 1200)," in html
    assert "candidate.memory_diffusion = {" in save_block
    assert "top_k: numberValue('cfg-diffusion-topk', 4)," in save_block
    assert "min_activation: floatValue('cfg-diffusion-min', 0.18)," in save_block
    assert "chain_walk_enabled: document.getElementById('cfg-chain-walk').value === 'true'," in save_block
    assert "chain_min_confidence: floatValue('cfg-chain-confidence', 0.72)," in save_block


def test_dashboard_config_save_sends_changed_sections_and_forces_memory_panel():
    html = Path("dashboard.html").read_text(encoding="utf-8")
    load_block = html.split("async function loadConfig()", 1)[1].split("async function saveConfig", 1)[0]
    save_block = html.split("async function saveConfig", 1)[1].split("try {", 1)[0]

    assert "let configSnapshot = null;" in html
    assert "configSnapshot = JSON.parse(JSON.stringify(cfg || {}));" in load_block
    assert "function addChangedSection(target, sectionName, candidate, force)" in save_block
    assert "if (force) {" in save_block
    assert "var activeTarget = activeTab ? activeTab.dataset.tab : '';" in save_block
    assert "var body = {" in save_block
    assert "if (persist) {" in save_block
    assert "Object.keys(candidate).forEach(function(sectionName) {" in save_block
    assert "forceSections[sectionName] = true;" in save_block
    assert "forceSections.gateway = true;" in save_block
    assert "forceSections.recall = true;" in save_block
    assert "forceSections.memory_diffusion = true;" in save_block
    assert "addChangedSection(body, sectionName, candidate[sectionName], !!forceSections[sectionName]);" in save_block
    assert "if (!body.persona) body.persona = {};" in save_block
    assert "body.persona.api_key = personaKeyVal;" in save_block


def test_dashboard_exposes_reflection_affect_anchor_switches():
    html = Path("dashboard.html").read_text(encoding="utf-8")
    load_block = html.split("async function loadConfig()", 1)[1].split("async function saveConfig", 1)[0]
    save_block = html.split("async function saveConfig", 1)[1].split("var keyVal =", 1)[0]

    assert "<h3>关系记忆整理</h3>" in html
    assert 'id="cfg-reflection-enabled"' in html
    assert 'id="cfg-reflection-auto"' in html
    assert 'id="cfg-reflection-memory-anchor"' in html
    assert 'id="cfg-reflection-weather-anchor"' in html
    assert 'id="cfg-reflection-candidate-model"' not in html
    assert 'id="cfg-reflection-candidate-thinking"' not in html
    assert 'id="cfg-reflection-model"' in html
    assert 'id="cfg-reflection-thinking"' in html
    assert 'id="cfg-reflection-url"' in html
    assert 'id="cfg-reflection-key"' in html
    assert 'data-tab="model-config"' in html
    assert 'id="model-config-view"' in html
    assert "<h3>自动记忆模型</h3>" in html
    assert "cfg.reflection.enabled" in load_block
    assert "cfg.reflection.auto_enabled" in load_block
    assert "cfg.reflection.memory_affect_anchor_enabled" in load_block
    assert "cfg.reflection.relationship_weather_affect_anchor_enabled" in load_block
    assert "cfg.reflection.daily_chat_memory_candidate_model" not in load_block
    assert "cfg.reflection.daily_chat_memory_candidate_thinking_mode" not in load_block
    config_block = save_block.split("if (activeTarget === 'config')", 1)[1].split("if (activeTarget === 'upstream-config')", 1)[0]
    model_block = save_block.split("if (activeTarget === 'model-config')", 1)[1].split("if (activeTarget === 'memory-config')", 1)[0]
    assert "enabled: document.getElementById('cfg-reflection-enabled').value === 'true'," in config_block
    assert "memory_affect_anchor_enabled: document.getElementById('cfg-reflection-memory-anchor').value === 'true'," in config_block
    assert "base_url: document.getElementById('cfg-reflection-url').value," in model_block
    assert "model: document.getElementById('cfg-reflection-model').value," in model_block
    assert "thinking_mode: document.getElementById('cfg-reflection-thinking').value," in model_block
    assert "daily_chat_memory_candidate_model" not in model_block
    assert "daily_chat_memory_candidate_thinking_mode" not in model_block
    assert "if (!body.reflection) body.reflection = {};" in html
    assert "body.reflection.api_key = reflectionKeyVal;" in html
    assert "body.gateway.domain_sentinel_api_key = domainSentinelKeyVal;" in html
    assert "activeTarget === 'model-config' && reflectionKeyVal" in html
    assert "activeTarget === 'model-config' && domainSentinelKeyVal" in html


def test_dashboard_exposes_chat_memory_tab_module():
    html = Path("dashboard.html").read_text(encoding="utf-8")
    module = Path("dashboard_assets/chat-memory.js").read_text(encoding="utf-8")

    assert 'data-tab="chat-memory"' in html
    assert 'id="chat-memory-view"' in html
    assert 'id="daily-chat-memory-pending"' in html
    assert "<h2>记忆候选</h2>" in html
    assert "chat-memory-board" in html
    assert "chat-memory-list" in html
    assert ".chat-memory-list {\n    display: flex;\n    flex-direction: column;" in html
    assert "repeat(auto-fit" not in html.split(".chat-memory-list", 1)[1].split(".chat-memory-card", 1)[0]
    assert "chat-memory-card" in module
    assert 'id="daily-chat-memory-date"' not in html
    assert 'id="daily-chat-memory-run-mode"' not in html
    assert "runDailyChatMemory" not in html
    assert "runDailyChatMemory" not in module
    assert "document.getElementById('chat-memory-view').style.display = target === 'chat-memory' ? '' : 'none';" in html
    assert "window.initDailyChatMemoryTab" in module
    assert "loadDashboardModule('/dashboard-assets/chat-memory.js');" in html
    assert "BASE + '/api/daily-chat-memory/run'" not in html
    assert "dailyChatMemoryApiBase() + '/api/daily-chat-memory/run'" not in module
    assert "dailyChatMemoryApiBase() + '/api/daily-chat-memory/pending?limit=20'" in module
    assert "dailyChatMemoryApiBase() + '/api/daily-chat-memory/confirm'" in module
    assert "data-field=\"title\"" in module
    assert "data-field=\"content\"" in module
    assert "body.edits[id] = edits;" in module


def test_dockerfile_copies_dashboard_assets():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert "COPY dashboard_assets ./dashboard_assets" in dockerfile


def test_dashboard_reflection_calendar_marks_events_and_sources():
    html = Path("dashboard.html").read_text(encoding="utf-8")

    assert "function getReflectionEventsByDate()" in html
    assert "function isReflectionEventBucket(bucket)" in html
    assert "reflection-day-dot event" in html
    assert "当天发生了什么" in html
    assert "reflectionSourceChips(detail, bucket.reflection_date)" in html
    assert "source_bucket_ids" in html
    assert "source_raw_event_ids" in html
    assert "return '';" in html.split("function getBucketEventDate", 1)[1].split("function isDailyImpressionBucket", 1)[0]


def test_dashboard_exposes_portrait_maintainer_controls():
    html = Path("dashboard.html").read_text(encoding="utf-8")
    load_block = html.split("async function loadConfig()", 1)[1].split("async function saveConfig", 1)[0]
    save_block = html.split("async function saveConfig", 1)[1].split("var keyVal =", 1)[0]

    assert "<h3>每日画像 Portrait</h3>" in html
    assert 'id="cfg-portrait-enabled"' in html
    assert 'id="cfg-portrait-auto"' in html
    assert 'id="cfg-portrait-auto-initial"' in html
    assert 'id="cfg-portrait-daily"' in html
    assert 'id="cfg-portrait-material-limit"' in html
    assert 'id="cfg-portrait-first-limit"' in html
    assert "cfg.portrait.enabled" in load_block
    assert "cfg.portrait.auto_enabled" in load_block
    assert "cfg.portrait.auto_initial_enabled" in load_block
    assert "cfg.portrait.daily_enabled" in load_block
    assert "cfg.portrait.first_run_material_limit" in load_block
    assert "Portrait API" in load_block
    portrait_block = save_block.split("candidate.portrait = {", 1)[1].split("};", 1)[0]
    assert "enabled: document.getElementById('cfg-portrait-enabled').value === 'true'," in portrait_block
    assert "auto_enabled: document.getElementById('cfg-portrait-auto').value === 'true'," in portrait_block
    assert "auto_initial_enabled: document.getElementById('cfg-portrait-auto-initial').value === 'true'," in portrait_block
    assert "daily_enabled: document.getElementById('cfg-portrait-daily').value === 'true'," in portrait_block
    assert "material_limit: numberValue('cfg-portrait-material-limit', 18)," in portrait_block
    assert "first_run_material_limit: numberValue('cfg-portrait-first-limit', 160)," in portrait_block
    assert "'portrait'," in save_block


def test_dashboard_exposes_persona_config_and_env_persist_button():
    html = Path("dashboard.html").read_text(encoding="utf-8")
    load_block = html.split("async function loadConfig()", 1)[1].split("async function saveConfig", 1)[0]
    save_block = html.split("async function saveConfig", 1)[1].split("var keyVal =", 1)[0]

    assert "<h3>Persona State</h3>" in html
    assert 'id="cfg-persona-enabled"' in html
    assert 'id="cfg-persona-event-recording"' in html
    assert 'id="cfg-persona-model"' in html
    assert 'id="cfg-persona-url"' in html
    assert 'id="cfg-persona-key"' in html
    assert "saveConfig(true, true)" in html
    assert "保存密钥到 .env" in html
    assert "cfg.persona.enabled" in load_block
    assert "cfg.persona.event_recording_enabled" in load_block
    assert "cfg.persona.api_key_masked" in load_block
    persona_block = save_block.split("candidate.persona = {", 1)[1].split("};", 1)[0]
    assert "enabled: document.getElementById('cfg-persona-enabled').value === 'true'," in persona_block
    assert "event_recording_enabled: document.getElementById('cfg-persona-event-recording').value === 'true'," in persona_block
    assert "model: document.getElementById('cfg-persona-model').value," in persona_block
    assert "base_url: document.getElementById('cfg-persona-url').value," in persona_block
    assert "persist_env: !!persistEnv" in save_block
    assert "if (!body.persona) body.persona = {};" in html
    assert "body.persona.api_key = personaKeyVal;" in html


def test_dashboard_exposes_gateway_upstream_editor():
    html = Path("dashboard.html").read_text(encoding="utf-8")
    load_block = html.split("async function loadConfig()", 1)[1].split("async function saveConfig", 1)[0]
    save_block = html.split("async function saveConfig", 1)[1].split("var keyVal =", 1)[0]
    upstream_view = html.split('id="upstream-config-view"', 1)[1].split('id="config-view"', 1)[0]
    config_view = html.split('id="config-view"', 1)[1].split('id="detail-panel"', 1)[0]

    assert 'data-tab="upstream-config"' in html
    assert 'id="upstream-config-view"' in html
    assert 'id="upstream-config-status"' in upstream_view
    assert "<h3>Gateway 上游模型</h3>" in html
    assert "<h3>Gateway 上游模型</h3>" not in config_view
    assert 'id="cfg-upstreams-list"' in upstream_view
    assert "function renderGatewayUpstreams()" in html
    assert "function gatewayCacheRetentionOptions(strategy, selected)" in html
    assert "function updateGatewayCacheRetentionOptions(index)" in html
    assert "function collectGatewayUpstreams(includeKeyValues)" in html
    assert "function collectGatewayUpstreamsForSave(includeKeyValues)" in html
    assert "gatewayUpstreams = (((cfg.gateway || {}).upstreams) || []).map(normalizeGatewayUpstream);" in load_block
    assert "var keyValuesText = upstream.api_key_values.join('\\n');" in html
    assert "var apiKeyValues = Array.isArray(raw.api_key_values)" in html
    assert "api_key_values: apiKeyValues.map(function(item) { return String(item || '').trim(); }).filter(Boolean)," in html
    assert "api_key_values: []," not in html
    assert "function updateGatewayKeyValueCount(index)" in html
    assert "updateGatewayKeyValueCount(index);" in html
    assert 'oninput="updateGatewayKeyValueCount(' in html
    assert 'id="cfg-upstream-key-values-count-\' + index + \'"' in html
    assert "当前读取: ' + splitSecretLines(keyValuesText).length + ' 行" in html
    assert "upstream.api_key_values = [];" in html
    assert "var upstreamKeyValueCounts = [];" in save_block
    assert "missingKeyValueProviders.push(upstream.name || '未命名上游');" in save_block
    assert "保存密钥到 .env 时，这些未 ready 上游的 Key values 读取到 0 行" in save_block
    assert "Key values 读取: " in html
    assert "document.getElementById('upstream-config-view').style.display = target === 'upstream-config' ? '' : 'none';" in html
    assert "if (target === 'upstream-config') loadConfig();" in html
    assert "if (activeTarget === 'upstream-config')" in save_block
    assert "var upstreamsResult = collectGatewayUpstreamsForSave(!!persistEnv);" in save_block
    assert "candidate.gateway = { upstreams: upstreamsResult.upstreams };" in save_block
    assert "api_key_values" in html
    assert 'onchange="updateGatewayCacheRetentionOptions(' in html
    assert 'id="cfg-upstream-cache-retention-' in html
    assert "current.prompt_cache_retention = current.prompt_cache ? value('cfg-upstream-cache-retention-' + index).trim() : '';" in html
    assert "choices.push({ value: '24h', label: '24h' });" in html
    assert "choices.push({ value: '1h', label: '1h' });" in html
    assert "填写了真实 key 时，请使用“应用并保存密钥到 .env”" in html
    assert "'upstream-config-status'" in html
    assert "document.querySelectorAll('[id^=\"cfg-upstream-key-values-\"]')" in html


def test_dashboard_dream_controls_load_and_save_runtime_fields():
    html = Path("dashboard.html").read_text(encoding="utf-8")
    load_block = html.split("async function loadConfig()", 1)[1].split("async function saveConfig", 1)[0]
    save_block = html.split("async function saveConfig", 1)[1].split("var keyVal =", 1)[0]
    dream_block = save_block.split("candidate.dream = {", 1)[1].split("};", 1)[0]
    dream_lines = [line.strip() for line in dream_block.splitlines()]

    assert 'id="cfg-dream-engine-enabled"' in html
    assert 'id="cfg-dream-inject"' in html
    assert 'id="cfg-dream-retain"' in html
    assert "document.getElementById('cfg-dream-engine-enabled').value = cfg.dream.enabled ? 'true' : 'false';" in load_block
    assert "document.getElementById('cfg-dream-enabled').value = cfg.dream.auto_enabled ? 'true' : 'false';" in load_block
    assert "document.getElementById('cfg-dream-inject').value = cfg.dream.inject_enabled ? 'true' : 'false';" in load_block
    assert "document.getElementById('cfg-dream-retain').value = cfg.dream.retain_after_inject ? 'true' : 'false';" in load_block
    assert "document.getElementById('cfg-dream-enabled').value = cfg.dream.enabled" not in load_block
    assert "base_url: document.getElementById('cfg-dream-url').value," in dream_lines
    assert "enabled: document.getElementById('cfg-dream-engine-enabled').value === 'true'," in dream_lines
    assert "model: document.getElementById('cfg-dream-model').value," in dream_lines
    assert "inject_enabled: document.getElementById('cfg-dream-inject').value === 'true'," in dream_lines
    assert "retain_after_inject: document.getElementById('cfg-dream-retain').value === 'true'," in dream_lines


def test_dashboard_config_number_zero_values_are_preserved():
    html = Path("dashboard.html").read_text(encoding="utf-8")
    load_block = html.split("async function loadConfig()", 1)[1].split("async function saveConfig", 1)[0]
    save_block = html.split("async function saveConfig", 1)[1].split("var keyVal =", 1)[0]

    assert "document.getElementById('cfg-dehy-temp').value = cfg.dehydration.temperature ?? 0.1;" in load_block
    assert "document.getElementById('cfg-merge').value = cfg.merge_threshold ?? 90;" in load_block
    assert "temperature: floatValue('cfg-dehy-temp', 0.1)," not in save_block
    assert "merge_threshold: numberValue('cfg-merge', 90)," not in save_block
    assert "cfg.dehydration.temperature || 0.1" not in load_block
    assert "cfg.merge_threshold || 90" not in load_block
    assert "parseFloat(document.getElementById('cfg-dehy-temp').value) || 0.1" not in save_block
    assert "parseInt(document.getElementById('cfg-merge').value) || 90" not in save_block


def test_dashboard_import_file_input_resets_after_selection():
    html = Path("dashboard.html").read_text(encoding="utf-8")
    import_block = html.split("// --- Import functions ---", 1)[1].split("async function pollImportStatus", 1)[0]

    assert "const selectedFile = fileInput.files[0];" in import_block
    assert "fileInput.value = '';" in import_block
    assert "startImport(selectedFile);" in import_block


def test_dashboard_import_upload_requests_resume():
    html = Path("dashboard.html").read_text(encoding="utf-8")
    import_block = html.split("async function startImport", 1)[1].split("let importPollTimer", 1)[0]

    assert "new URLSearchParams" in import_block
    assert "preserve_raw: preserveRaw ? '1' : '0'," in import_block
    assert "resume: '1'," in import_block
    assert "'/api/import/upload?' + params.toString()" in import_block
