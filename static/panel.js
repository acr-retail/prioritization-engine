// ---- Shared state ----
const taskMap = {};
let fieldOptions = null;
let currentTaskId = null;
let panelOriginalValues = {};

// Score thresholds (percentile-based, loaded from server)
let scoreThresholds = {critical: 10, high: 20, medium: 30};
try {
    const td = document.getElementById('thresholdData');
    if (td) scoreThresholds = JSON.parse(td.textContent);
} catch(e) {}

function getScoreClass(score) {
    if (score < scoreThresholds.critical) return 'score-critical';
    if (score < scoreThresholds.high) return 'score-high';
    if (score < scoreThresholds.medium) return 'score-medium';
    return 'score-low';
}

// ---- Cache helpers ----
const CACHE_TTL = 5 * 60 * 1000; // 5 minutes

function cacheGet(key) {
    try {
        const raw = localStorage.getItem(key);
        if (!raw) return null;
        const {data, ts} = JSON.parse(raw);
        if (Date.now() - ts > CACHE_TTL) { localStorage.removeItem(key); return null; }
        return data;
    } catch { return null; }
}

function cacheSet(key, data) {
    try { localStorage.setItem(key, JSON.stringify({data, ts: Date.now()})); } catch {}
}

// Load dropdown options — from cache first, then refresh in background
fieldOptions = cacheGet('fieldOptions');
fetch('/api/options')
    .then(r => r.json())
    .then(data => { fieldOptions = data; cacheSet('fieldOptions', data); })
    .catch(err => console.error('Failed to load options:', err));

// ---- Panel open/close ----
function openPanel(taskId) {
    currentTaskId = taskId;
    document.getElementById('panelTitle').textContent = '#' + taskId;
    document.getElementById('panelScore').innerHTML = '';
    document.getElementById('panelBody').innerHTML =
        '<div style="text-align:center;padding:3rem;color:#94a3b8;">Loading...</div>';
    document.getElementById('panelOverlay').classList.add('open');
    document.getElementById('detailPanel').classList.add('open');

    fetch('/api/task/' + taskId)
        .then(r => { if (!r.ok) throw new Error('Failed to load'); return r.json(); })
        .then(data => renderPanel(data.task, data.messages))
        .catch(err => {
            document.getElementById('panelBody').innerHTML =
                '<div class="error">' + escHtml(err.message) + '</div>';
        });
}

function openTicketPanel(ticketId) {
    document.getElementById('panelTitle').textContent = '#' + ticketId;
    document.getElementById('panelScore').innerHTML = '';
    document.getElementById('panelBody').innerHTML =
        '<div style="text-align:center;padding:3rem;color:#94a3b8;">Loading...</div>';
    document.getElementById('panelOverlay').classList.add('open');
    document.getElementById('detailPanel').classList.add('open');

    fetch('/api/ticket/detail/' + ticketId)
        .then(r => { if (!r.ok) throw new Error('Failed to load'); return r.json(); })
        .then(data => renderTicketPanel(data.ticket, data.messages))
        .catch(err => {
            document.getElementById('panelBody').innerHTML =
                '<div class="error">' + escHtml(err.message) + '</div>';
        });
}

function renderTicketPanel(ticket, messages) {
    const opts = fieldOptions || {};

    const score = ticket._score || 0;
    document.getElementById('panelScore').innerHTML =
        '<span class="score ' + getScoreClass(score) + '">' + score + '</span>' +
        '<span style="color:#64748b;font-size:0.85rem;">Priority Score</span>';

    let html = '<div class="panel-field"><div class="panel-field-label">Title</div>' +
        '<div class="panel-field-value">' + escHtml(ticket.name || '') + '</div></div>';

    const stage = ticket.stage_id ? (Array.isArray(ticket.stage_id) ? ticket.stage_id[1] : ticket.stage_id) : '—';
    const customer = ticket.partner_id ? (Array.isArray(ticket.partner_id) ? ticket.partner_id[1] : ticket.partner_id) : '—';
    const assignee = ticket.user_id ? (Array.isArray(ticket.user_id) ? ticket.user_id[1] : ticket.user_id) : '—';

    html += '<div class="panel-grid">';
    html += panelReadonly('Status', stage, 'Current ticket stage.');
    html += panelReadonly('Customer', customer, 'The customer this ticket is for.');
    html += panelReadonly('Assignee', assignee, 'Who is handling this ticket.');
    html += panelReadonly('Issue Type', ticket.x_studio_customer_impact || '—', 'Classification of the issue.');
    html += panelReadonly('Escalated', ticket.x_studio_escalated ? 'Yes' : 'No', 'Whether this ticket has been escalated.');
    html += panelReadonly('Customer Funded', ticket.x_studio_customer_funded || '—', 'Whether the customer is paying for this.');
    html += panelReadonly('Paid Prioritization', ticket.x_studio_paid_prioritization ? 'Yes' : 'No', 'Whether paid priority was requested.');
    html += panelReadonly('Ticket Ref', ticket.ticket_ref || '—', 'Bugzilla ticket number.');
    html += panelReadonly('Created', ticket.create_date ? ticket.create_date.slice(0, 10) : '—', 'Date created.');
    html += '</div>';

    // Description
    if (ticket.description && ticket.description !== '<p><br></p>' && ticket.description !== false) {
        html += '<div style="margin-top:1.5rem;padding-top:1rem;border-top:1px solid #e2e8f0;">' +
            '<div class="panel-field-label">Description</div>' +
            '<div style="font-size:0.85rem;color:#374151;line-height:1.6;">' + ticket.description + '</div></div>';
    }

    // Messages
    const msgCount = messages ? messages.length : 0;
    html += '<div style="margin-top:1.5rem;padding-top:1rem;border-top:1px solid #e2e8f0;">' +
        '<div class="panel-field-label" style="margin-bottom:0.75rem;">Conversation (<span id="conversationCount">' + msgCount + '</span>)</div>' +
        '<div class="conversation-list">';
    if (messages) messages.forEach(msg => html += renderMessage(msg));
    html += '</div></div>';

    // Comment box
    html += '<div style="margin-top:1rem;"><textarea id="commentBox" rows="3" class="panel-input" ' +
        'style="resize:vertical;min-height:60px;" placeholder="Write a comment..."></textarea>' +
        '<div style="display:flex;gap:0.5rem;margin-top:0.5rem;align-items:center;">' +
        '<button class="btn btn-primary btn-sm" onclick="postComment(\'ticket\',' + ticket.id + ')">Post Comment</button>' +
        '<span id="commentStatus" style="font-size:0.75rem;color:#94a3b8;"></span></div></div>';

    // Link to Odoo
    html += '<div style="margin-top:1.5rem;padding-top:1rem;border-top:1px solid #e2e8f0;">' +
        '<a href="https://odoo-ps-psus-all-about-technology-sandbox-30173849.dev.odoo.com/web#id=' +
        ticket.id + '&model=helpdesk.ticket&view_type=form" target="_blank" ' +
        'class="btn btn-ghost" style="width:100%;justify-content:center;">Open Ticket in Odoo →</a></div>';

    document.getElementById('panelBody').innerHTML = html;
}

function openProjectPanel(projectId) {
    document.getElementById('panelTitle').textContent = 'Project';
    document.getElementById('panelScore').innerHTML = '';
    document.getElementById('panelBody').innerHTML =
        '<div style="text-align:center;padding:3rem;color:#94a3b8;">Loading...</div>';
    document.getElementById('panelOverlay').classList.add('open');
    document.getElementById('detailPanel').classList.add('open');

    fetch('/api/project/' + projectId)
        .then(r => { if (!r.ok) throw new Error('Failed to load'); return r.json(); })
        .then(data => renderProjectPanel(data.project, data.tasks))
        .catch(err => {
            document.getElementById('panelBody').innerHTML =
                '<div class="error">' + escHtml(err.message) + '</div>';
        });
}

function renderProjectPanel(project, tasks) {
    document.getElementById('panelTitle').textContent = project.name;
    document.getElementById('panelScore').innerHTML =
        '<span style="font-size:0.85rem;color:#64748b;">' + tasks.length + ' open tasks</span>';

    let html = '';

    // Project info
    const manager = project.user_id ? (Array.isArray(project.user_id) ? project.user_id[1] : project.user_id) : '—';
    const customer = project.partner_id ? (Array.isArray(project.partner_id) ? project.partner_id[1] : project.partner_id) : '—';

    html += '<div class="panel-grid">';
    html += panelReadonly('Manager', manager, 'Project manager in Odoo.');
    html += panelReadonly('Customer', customer, 'Customer associated with this project.');
    html += panelReadonly('Start Date', project.date_start || '—', 'Project start date.');
    html += panelReadonly('End Date', project.date || '—', 'Project end date.');
    html += panelReadonly('Total Tasks', project.task_count || 0, 'Total tasks in this project (including closed).');
    html += panelReadonly('Open Tasks', tasks.length, 'Currently open tasks.');
    html += '</div>';

    // Description
    if (project.description && project.description !== '<p><br></p>' && project.description !== false) {
        html += '<div style="margin-top:1.5rem;padding-top:1rem;border-top:1px solid #e2e8f0;">' +
            '<div class="panel-field-label">Description</div>' +
            '<div style="font-size:0.85rem;color:#374151;line-height:1.6;">' + project.description + '</div></div>';
    }

    // Task list
    if (tasks.length > 0) {
        html += '<div style="margin-top:1.5rem;padding-top:1rem;border-top:1px solid #e2e8f0;">' +
            '<div class="panel-field-label" style="margin-bottom:0.75rem;">Open Tasks (' + tasks.length + ')</div>';

        tasks.forEach(t => {
            const cls = getScoreClass(t._score);
            html += '<div style="display:flex;align-items:center;gap:0.5rem;padding:0.4rem 0;border-bottom:1px solid #f1f5f9;cursor:pointer;" ' +
                'onclick="openPanel(' + t.id + ')">' +
                '<span class="score ' + cls + '" style="font-size:0.7rem;min-width:24px;">' + t._score + '</span>' +
                '<span style="font-size:0.8rem;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + escHtml(t.name) + '</span>' +
                '<span class="badge badge-gray" style="font-size:0.65rem;">' + escHtml(t._stage) + '</span>' +
                '</div>';
        });

        html += '</div>';
    }

    // Link to Odoo
    html += '<div style="margin-top:1.5rem;padding-top:1rem;border-top:1px solid #e2e8f0;">' +
        '<a href="https://odoo-ps-psus-all-about-technology-sandbox-30173849.dev.odoo.com/web#id=' +
        project.id + '&model=project.project&view_type=form" target="_blank" ' +
        'class="btn btn-ghost" style="width:100%;justify-content:center;">Open Project in Odoo →</a></div>';

    document.getElementById('panelBody').innerHTML = html;
}

function closePanel() {
    document.getElementById('panelOverlay').classList.remove('open');
    document.getElementById('detailPanel').classList.remove('open');
}

document.addEventListener('keydown', e => { if (e.key === 'Escape') closePanel(); });

// ---- Render panel ----
function renderPanel(task, messages) {
    currentTaskId = task.id;
    const opts = fieldOptions || {};

    document.getElementById('panelScore').innerHTML =
        '<span class="score ' + getScoreClass(task._score) + '">' + task._score + '</span>' +
        '<span style="color:#64748b;font-size:0.85rem;">Priority Score — lower is more urgent</span>';

    let html = '<form id="panelForm" onsubmit="return savePanel(event)">';
    html += '<div class="panel-field"><div class="panel-field-label">Title</div>' +
        '<input type="text" name="name" value="' + escAttr(task.name || '') + '" class="panel-input"></div>';
    html += '<div class="panel-grid">';

    const hasTicket = !!(task.helpdesk_ticket_id && task.helpdesk_ticket_id !== false);

    // Task-level fields (always shown)
    const stageId = task.stage_id ? (Array.isArray(task.stage_id) ? task.stage_id[0] : task.stage_id) : '';
    html += panelSelect('Status', 'stage_id', stageId, opts.stages || [], 'id', 'name',
        'Current workflow stage of the task.');
    const assigneeId = task.user_ids && task.user_ids.length > 0 ? task.user_ids[0] : '';
    html += panelSelect('Assignee', 'user_id', assigneeId, opts.users || [], 'id', 'name',
        'The developer assigned to this task. Used for Gantt grouping.');
    html += panelSelect('Level of Effort', 'x_studio_level_of_effort', task.x_studio_level_of_effort || '', opts.effort_levels || [], 'value', 'label',
        'Estimated work hours. Sets the minimum duration on the Gantt chart.');
    html += panelBool('Roadmap', 'x_studio_road_map_flag', task.x_studio_road_map_flag,
        'Roadmap items are prioritized higher for planned development.');
    html += panelSelect('Priority', 'priority', task.priority || '0', opts.priorities || [], 'value', 'label',
        'Odoo priority level (Low/Medium/High/Urgent).');

    // Ticket-level fields (only shown if linked helpdesk ticket exists)
    if (hasTicket) {
        const custId = task.x_studio_customer ? (Array.isArray(task.x_studio_customer) ? task.x_studio_customer[0] : task.x_studio_customer) : '';
        html += panelSelect('Customer', 'x_studio_customer', custId, opts.customers || [], 'id', 'name',
            'The customer this task is for. Affects priority score.');
        html += panelSelect('Issue Type', 'x_studio_issue_type', task.x_studio_issue_type || '', opts.issue_types || [], 'value', 'label',
            'Classification of the issue. System-stopping bugs score highest priority.');
        html += panelBool('Escalated', 'x_studio_related_field_5vi_1jnfmj9cf', task.x_studio_related_field_5vi_1jnfmj9cf,
            'Escalated items receive a higher priority score.');
        html += panelSelect('Customer Funded', 'x_studio_related_field_gd_1jnftb4gl', task.x_studio_related_field_gd_1jnftb4gl || '', opts.customer_funded || [], 'value', 'label',
            'Customer-funded items are boosted in priority.');
        html += panelBool('Paid Prioritization', 'x_studio_related_field_27d_1jnftbs3p', task.x_studio_related_field_27d_1jnftbs3p,
            'Paid items receive the highest priority boost (-10 weight).');
    }
    html += panelReadonly('Age', task._age || '—',
        'Days since creation. Older items score higher priority.');
    html += panelReadonly('Created', task.create_date ? task.create_date.slice(0, 10) : '—',
        'Date the task was created. Cannot be changed.');
    html += panelDate('Start Date', 'planned_date_begin', task.planned_date_begin ? task.planned_date_begin.slice(0, 10) : '',
        'When work begins. Defines the Gantt bar start position.');
    html += panelDate('End Date', 'date_end', task.date_end ? task.date_end.slice(0, 10) : '',
        'When work is expected to finish. Defines the Gantt bar length.');
    html += panelDate('Deadline', 'date_deadline', task.date_deadline ? task.date_deadline.slice(0, 10) : '',
        'Hard constraint. The Gantt bar cannot be moved or extended past this date.');
    html += panelDate('Assigned', 'date_assign', task.date_assign ? task.date_assign.slice(0, 10) : '',
        'Date the task was assigned to the current developer.');
    html += '<div class="panel-field"><div class="panel-field-label">Time Allocated (hrs)' + tipHtml('Budgeted hours for this task.') + '</div>' +
        '<input type="number" name="allocated_hours" step="0.5" min="0" value="' + (task.allocated_hours || '') + '" class="panel-input" placeholder="—"></div>';
    html += panelReadonly('Time Spent', task.effective_hours ? task.effective_hours.toFixed(1) + 'h' : '—',
        'Actual hours logged via timesheets. Read-only.');
    html += '</div>';
    html += '<div style="margin-top:1.25rem;display:flex;gap:0.75rem;">' +
        '<button type="submit" class="btn btn-primary" id="panelSaveBtn">Save Changes</button>' +
        '<span id="panelSaveStatus" style="font-size:0.8rem;color:#16a34a;align-self:center;"></span></div>';
    html += '</form>';

    // Description — always show full content
    html += '<div style="margin-top:1.5rem;padding-top:1rem;border-top:1px solid #e2e8f0;"><div class="panel-field-label">Description</div>' +
        '<div style="font-size:0.85rem;color:#374151;line-height:1.6;">' +
        (task.description && task.description !== '<p><br></p>' && task.description !== false ? task.description : '<span style="color:#94a3b8;">No description</span>') +
        '</div></div>';

    const msgCount = messages ? messages.length : 0;
    html += '<div style="margin-top:1.5rem;padding-top:1rem;border-top:1px solid #e2e8f0;">' +
        '<div class="panel-field-label" style="margin-bottom:0.75rem;">Conversation (<span id="conversationCount">' + msgCount + '</span>)</div><div class="conversation-list">';
    if (messages) messages.forEach(msg => html += renderMessage(msg));
    html += '</div></div>';

    html += '<div style="margin-top:1rem;"><textarea id="commentBox" rows="3" class="panel-input" style="resize:vertical;min-height:60px;" placeholder="Write a comment..."></textarea>' +
        '<div style="display:flex;gap:0.5rem;margin-top:0.5rem;align-items:center;">' +
        '<button class="btn btn-primary btn-sm" onclick="postComment(\'task\',' + task.id + ')">Post Comment</button>' +
        '<span id="commentStatus" style="font-size:0.75rem;color:#94a3b8;"></span></div></div>';

    html += '<div style="margin-top:1.5rem;padding-top:1rem;border-top:1px solid #e2e8f0;">' +
        '<a href="https://odoo-ps-psus-all-about-technology-sandbox-30173849.dev.odoo.com/web#id=' +
        task.id + '&model=project.task&view_type=form" target="_blank" class="btn btn-ghost" style="width:100%;justify-content:center;">Open in Odoo →</a></div>';

    document.getElementById('panelBody').innerHTML = html;
    capturePanelInitialValues();
}

// Snapshot the form's current values so savePanel can compute a diff
// and only send changed fields. Called whenever the panel renders.
function capturePanelInitialValues() {
    panelOriginalValues = {};
    const form = document.getElementById('panelForm');
    if (!form) return;
    new FormData(form).forEach((v, k) => {
        panelOriginalValues[k] = normalizeFormValue(v);
    });
}

function normalizeFormValue(v) {
    if (v === 'true') return true;
    if (v === 'false') return false;
    return v;
}

// ---- Helpers ----
function tipHtml(tip) {
    if (!tip) return '';
    return ' <span class="panel-tooltip" title="' + escAttr(tip) + '">?</span>';
}

function panelSelect(label, name, currentVal, options, valKey, labelKey, tip) {
    let h = '<div class="panel-field"><div class="panel-field-label">' + label + tipHtml(tip) + '</div><select name="' + name + '" class="panel-input"><option value="">—</option>';
    for (const opt of options) { const v = String(opt[valKey]); h += '<option value="' + escAttr(v) + '"' + (String(currentVal) === v ? ' selected' : '') + '>' + escHtml(opt[labelKey]) + '</option>'; }
    return h + '</select></div>';
}

function panelBool(label, name, val, tip) {
    return '<div class="panel-field"><div class="panel-field-label">' + label + tipHtml(tip) + '</div><select name="' + name + '" class="panel-input">' +
        '<option value="false"' + (val !== true ? ' selected' : '') + '>No</option>' +
        '<option value="true"' + (val === true ? ' selected' : '') + '>Yes</option></select></div>';
}

function panelDate(label, name, val, tip) {
    return '<div class="panel-field"><div class="panel-field-label">' + label + tipHtml(tip) + '</div>' +
        '<input type="date" name="' + name + '" value="' + escAttr(val) + '" class="panel-input"></div>';
}

function panelReadonly(label, val, tip) {
    return '<div class="panel-field"><div class="panel-field-label">' + label + tipHtml(tip) + '</div>' +
        '<div class="panel-field-value">' + escHtml(String(val)) + '</div></div>';
}

function escHtml(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

function escAttr(s) { return s.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

function renderMessage(msg) {
    const d = msg.date ? msg.date.slice(0, 16).replace('T', ' ') : '';
    const a = msg._author || 'System';
    const icon = msg.message_type === 'email' ? '✉' : msg.message_type === 'comment' ? '💬' : '🔔';
    let h = '<div style="margin-bottom:1rem;padding-bottom:1rem;border-bottom:1px solid #f1f5f9;">' +
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.35rem;">' +
        '<span style="font-weight:600;font-size:0.8rem;color:#1a1a1a;">' + icon + ' ' + escHtml(a) + '</span>' +
        '<span style="font-size:0.7rem;color:#94a3b8;">' + escHtml(d) + '</span></div>' +
        '<div style="font-size:0.8rem;color:#475569;line-height:1.5;overflow-wrap:break-word;">' + msg.body + '</div>';
    if (msg._attachments && msg._attachments.length > 0) {
        h += '<div style="margin-top:0.4rem;">';
        msg._attachments.forEach(att => {
            const sz = att.file_size ? ' (' + (att.file_size / 1024).toFixed(0) + ' KB)' : '';
            h += '<span style="display:inline-block;font-size:0.7rem;color:#2563eb;background:#eff6ff;padding:0.15rem 0.4rem;border-radius:3px;margin-right:0.3rem;">📎 ' + escHtml(att.name) + sz + '</span>';
        });
        h += '</div>';
    }
    return h + '</div>';
}

// ---- Refresh filter dropdowns after changes ----
function refreshFilters() {
    const table = document.getElementById('backlog-table');
    if (!table) return;
    const selects = table.querySelectorAll('.filter-row select');
    selects.forEach(select => {
        const col = parseInt(select.dataset.col);
        if (isNaN(col)) return;
        const currentVal = select.value;
        const values = new Set();
        table.querySelectorAll('tbody tr').forEach(row => {
            const t = row.cells[col]?.textContent.trim();
            if (t) values.add(t);
        });
        const sorted = Array.from(values).sort((a, b) => {
            const na = parseFloat(a), nb = parseFloat(b);
            if (!isNaN(na) && !isNaN(nb)) return na - nb;
            return a.localeCompare(b);
        });
        select.innerHTML = '<option value="">All</option>';
        sorted.forEach(val => {
            const o = document.createElement('option');
            o.value = val; o.textContent = val;
            if (val === currentVal) o.selected = true;
            select.appendChild(o);
        });
    });
}

// ---- Post-save hooks (pages can override these) ----
// Called after a task is saved with the refreshed task data from the API
function onTaskUpdated(task) {
    // Update taskMap
    taskMap[task.id] = Object.assign(taskMap[task.id] || {}, task);

    // Update backlog table row if it exists
    updateBacklogRow(task);

    // Update gantt bar if it exists
    updateGanttBar(task);

    // Refresh filter dropdowns
    refreshFilters();
}

// Full row repaint after a save. Column order in backlog.html:
//   0:Score 1:Groom 2:ID 3:Title 4:Customer 5:Status 6:IssueType 7:Esc
//   8:Funded 9:Effort 10:Paid 11:Roadmap 12:Assignee 13:Age 14:Created
function updateBacklogRow(task) {
    const row = document.querySelector('tr[data-task-id="' + task.id + '"]');
    if (!row || !row.cells || row.cells.length < 15) return;
    const c = row.cells;

    // 0: Score
    c[0].innerHTML = '<span class="score ' + getScoreClass(task._score) + '">' + task._score + '</span>';

    // 1: Groom
    const g = task._grooming || {};
    if (g.groomed === false) {
        c[1].innerHTML = '<span style="background:#f5f3ff;color:#8b5cf6;padding:0.1rem 0.4rem;border-radius:9999px;font-weight:600;font-size:0.65rem;" data-ungroomed="true">' + (g.missing_count || 0) + ' missing</span>';
        c[1].title = (g.missing || []).join(', ');
    } else {
        c[1].innerHTML = '<span style="color:#d4d4d8;">✓</span>';
        c[1].title = 'Fully groomed';
    }

    // 3: Title — preserve truncation styling
    const titleAnchor = c[3].querySelector('.truncate') || c[3];
    titleAnchor.textContent = task.name || '';
    if (titleAnchor.setAttribute) titleAnchor.setAttribute('title', task.name || '');

    // 4: Customer
    c[4].innerHTML = task._customer
        ? '<span class="badge badge-blue">' + escHtml(task._customer) + '</span>'
        : '';

    // 5: Status
    const stage = task._stage || '';
    let stageBadge = 'badge-gray';
    if (stage.includes('Progress')) stageBadge = 'badge-yellow';
    else if (stage.includes('Queued')) stageBadge = 'badge-blue';
    c[5].innerHTML = '<span class="badge ' + stageBadge + '">' + escHtml(stage) + '</span>';

    // 6: Issue Type — show the resolved label (Emily's fix path)
    c[6].textContent = task._issue_type_label || '';

    // 7: Escalated
    c[7].innerHTML = task.x_studio_related_field_5vi_1jnfmj9cf
        ? '<span class="badge badge-yellow">Yes</span>'
        : '<span style="color:#cbd5e1;">No</span>';

    // 8: Funded — only "Yes" gets the green badge
    const funded = task.x_studio_related_field_gd_1jnftb4gl || '';
    c[8].innerHTML = funded === 'Yes'
        ? '<span class="badge badge-green">Yes</span>'
        : '<span style="color:#cbd5e1;">' + escHtml(funded || 'No') + '</span>';

    // 9: Effort
    c[9].textContent = task.x_studio_level_of_effort || '';

    // 10: Paid
    c[10].innerHTML = task.x_studio_related_field_27d_1jnftbs3p
        ? '<span class="badge badge-yellow">Yes</span>'
        : '<span style="color:#cbd5e1;">No</span>';

    // 11: Roadmap
    c[11].innerHTML = task.x_studio_road_map_flag
        ? '<span class="badge badge-blue">Yes</span>'
        : '<span style="color:#cbd5e1;">No</span>';

    // 12: Assignee
    if (task._assignee && task._assignee !== 'Unassigned') {
        c[12].innerHTML = '<span class="badge badge-gray">' + escHtml(task._assignee) + '</span>';
    } else {
        c[12].innerHTML = '<span style="color:#cbd5e1;">Unassigned</span>';
    }

    // 13: Age
    const age = task._age || '';
    let ageBadge = 'badge-gray';
    if (age === '>90') ageBadge = 'badge-yellow';
    else if (age === '60-90') ageBadge = 'badge-blue';
    c[13].innerHTML = '<span class="badge ' + ageBadge + '">' + escHtml(age) + '</span>';
}

function updateGanttBar(task) {
    const bar = document.querySelector('.gantt-bar[data-task-id="' + task.id + '"]');
    if (!bar) return;

    const col = (task._assignee && task._assignee !== 'Unassigned') ? '#93b5e6' : '#e57373';
    bar.style.background = col;

    // If rebuildGantt exists (we're on the gantt page), rebuild to recalculate positions
    if (typeof rebuildGantt === 'function') {
        rebuildGantt();
    }
}

// ---- Save (dirty-tracking) ----
// Sends only fields whose value differs from the snapshot captured when
// the panel rendered. Avoids overwriting concurrent edits and avoids
// wasteful Odoo writes for fields the user didn't touch.
function savePanel(e) {
    e.preventDefault();
    const form = document.getElementById('panelForm');
    const btn = document.getElementById('panelSaveBtn');
    const status = document.getElementById('panelSaveStatus');

    const diff = {};
    new FormData(form).forEach((v, k) => {
        const current = normalizeFormValue(v);
        const initial = panelOriginalValues[k];
        if (current !== initial) {
            diff[k] = current;
        }
    });

    if (Object.keys(diff).length === 0) {
        status.textContent = '✓ No changes';
        status.style.color = '#64748b';
        setTimeout(() => status.textContent = '', 2000);
        return false;
    }

    btn.disabled = true; btn.textContent = 'Saving...'; status.textContent = '';

    fetch('/api/task/' + currentTaskId + '/update', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(diff) })
        .then(r => { if (!r.ok) return r.json().then(d => { throw new Error(d.detail || 'Failed'); }); return r.json(); })
        .then(() => {
            btn.disabled = false; btn.textContent = 'Save Changes';
            status.textContent = '✓ Saved (' + Object.keys(diff).length + ')';
            status.style.color = '#16a34a';
            setTimeout(() => status.textContent = '', 2000);

            // Pull the refreshed task back — score, related fields,
            // and any Odoo-side recomputation are now reflected.
            return fetch('/api/task/' + currentTaskId).then(r => r.json());
        })
        .then(taskData => {
            if (taskData && taskData.task) {
                onTaskUpdated(taskData.task);
                // The form values we just saved are the new "initial" —
                // capture them so further edits diff from this point.
                capturePanelInitialValues();
            }
        })
        .catch(err => {
            btn.disabled = false; btn.textContent = 'Save Changes';
            status.textContent = '✗ ' + err.message; status.style.color = '#dc2626';
        });
    return false;
}

// ---- Post Comment ----
function postComment(model, recordId) {
    const box = document.getElementById('commentBox');
    const status = document.getElementById('commentStatus');
    const body = box.value.trim();
    if (!body) return;
    const now = new Date().toISOString().slice(0, 16).replace('T', ' ');
    const user = document.querySelector('.user')?.textContent || 'You';
    const list = document.querySelector('.conversation-list');
    if (list) {
        const el = document.createElement('div');
        el.style.cssText = 'margin-bottom:1rem;padding-bottom:1rem;border-bottom:1px solid #f1f5f9;';
        el.innerHTML = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.35rem;">' +
            '<span style="font-weight:600;font-size:0.8rem;">💬 ' + escHtml(user) + '</span>' +
            '<span style="font-size:0.7rem;color:#94a3b8;">' + now + '</span></div>' +
            '<div style="font-size:0.8rem;color:#475569;line-height:1.5;">' + escHtml(body).replace(/\n/g, '<br/>') + '</div>';
        list.insertBefore(el, list.firstChild);
        const cnt = document.getElementById('conversationCount');
        if (cnt) cnt.textContent = parseInt(cnt.textContent) + 1;
    }
    box.value = ''; status.textContent = 'Posting...'; status.style.color = '#94a3b8';
    fetch('/api/' + model + '/' + recordId + '/comment', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ body }) })
        .then(r => { if (!r.ok) return r.json().then(d => { throw new Error(d.detail); }); return r.json(); })
        .then(() => { status.textContent = '✓ Saved'; status.style.color = '#16a34a'; setTimeout(() => status.textContent = '', 2000); })
        .catch(err => { status.textContent = '✗ ' + err.message; status.style.color = '#dc2626'; });
}

// ---- Presence ----
setInterval(() => fetch('/api/presence/heartbeat', { method: 'POST' }).catch(() => {}), 30000);
fetch('/api/presence/heartbeat', { method: 'POST' }).catch(() => {});

let lastPresenceHtml = cacheGet('presenceHtml');
function renderPresenceHtml(el) { if (lastPresenceHtml) el.innerHTML = lastPresenceHtml; }

// Show cached presence immediately
(function() {
    const el = document.getElementById('presenceIndicator');
    if (el && lastPresenceHtml) el.innerHTML = lastPresenceHtml;
})();

function updatePresence() {
    fetch('/api/presence/online').then(r => r.json()).then(data => {
        const el = document.getElementById('presenceIndicator');
        if (!el) return;
        if (!data.users.length) { el.innerHTML = ''; lastPresenceHtml = ''; cacheSet('presenceHtml', ''); return; }
        let h = '';
        data.users.forEach((login, i) => {
            h += '<img src="https://api.dicebear.com/7.x/initials/svg?seed=' + encodeURIComponent(login) +
                '&backgroundColor=c0aede,d1d4f9,b6e3f4,ffd5dc,ffdfbf" ' +
                'style="width:28px;height:28px;border-radius:50%;border:2px solid #fff;' +
                (i > 0 ? 'margin-left:-8px;' : '') + 'box-shadow:0 1px 3px rgba(0,0,0,0.15);" title="' + escAttr(login) + '">';
        });
        h += '<span style="font-size:0.7rem;color:#64748b;margin-left:0.4rem;">' + data.users.length + ' online</span>';
        el.innerHTML = h;
        lastPresenceHtml = h;
        cacheSet('presenceHtml', h);
    }).catch(() => {});
}
setInterval(updatePresence, 30000);
updatePresence();
