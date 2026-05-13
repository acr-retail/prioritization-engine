// Generic multi-select dropdown widget.
//
// Wraps a hidden <select> element with a button + popover that contains
// checkboxes for the listed options. The hidden <select>'s .value is kept
// in sync as a comma-separated string ("Bug,Enhancement") so any existing
// filter-apply pipeline that already reads select.value works unchanged.
//
// Used on the backlog filter row and on the gantt page's top filter bar.
// CSS lives in templates/base.html (.multi-select-wrapper, -button,
// -popover, -item, -divider, .active-filter).
//
// Parameters:
//   select        — the <select> element to wrap (kept in DOM, hidden)
//   options       — [{value, label}] of regular checkbox items
//   opts.specials — optional [{value, label}] of "sentinel" rows shown
//                   above a divider; selecting one clears all others
//                   (used for the tag column's All-Tagged / Untagged).
//                   Pass [] / omit for normal columns.
//   opts.label    — aria-label used on the button (e.g. "Filter by status")
//   opts.initial  — initial comma-separated value to seed selection with

// Internal delimiter used to join multiple selected values inside the
// hidden <select>'s .value. Cannot be comma because real data contains
// commas (e.g. "Schnuck Markets, Inc., Michael Wait"). \x1f is the ASCII
// Unit Separator — guaranteed never to appear in display strings.
const MS_SEP = '\x1f';

function mountMultiSelect(select, options, opts) {
    options = options || [];
    const specials = (opts && opts.specials) || [];
    const ariaLabel = (opts && opts.label) || 'Filter';
    const initialValue = (opts && opts.initial) || '';

    select.style.display = 'none';
    select.setAttribute('aria-hidden', 'true');
    select.tabIndex = -1;

    // Native <select>.value only accepts values that exist as an <option>.
    // Since our state is an arbitrary comma-separated string, we (re)create
    // a single matching option every time the state changes. The select
    // still drives the filter pipeline via .value.
    function syncSelectValue(value) {
        select.innerHTML = '';
        const all = document.createElement('option');
        all.value = '';
        select.appendChild(all);
        if (value) {
            const o = document.createElement('option');
            o.value = value;
            o.selected = true;
            select.appendChild(o);
        }
    }
    syncSelectValue(initialValue);

    const wrapper = document.createElement('div');
    wrapper.className = 'multi-select-wrapper';

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'multi-select-button';
    button.setAttribute('aria-haspopup', 'listbox');
    button.setAttribute('aria-expanded', 'false');
    button.setAttribute('aria-label', ariaLabel);

    const popover = document.createElement('div');
    popover.className = 'multi-select-popover';
    popover.setAttribute('role', 'listbox');
    popover.setAttribute('aria-multiselectable', 'true');
    popover.setAttribute('aria-label', ariaLabel + ' options');
    popover.style.display = 'none';

    wrapper.appendChild(button);
    wrapper.appendChild(popover);
    select.parentNode.insertBefore(wrapper, select);
    wrapper.appendChild(select);

    let isOpen = false;
    const specialValues = new Set(specials.map(s => s.value));

    function getSelected() {
        return select.value ? select.value.split(MS_SEP).filter(Boolean) : [];
    }
    function setSelected(arr) {
        const next = arr.join(MS_SEP);
        if (select.value === next) return;
        syncSelectValue(next);
        select.dispatchEvent(new Event('change', { bubbles: true }));
        updateButton();
        updateChecks();
    }
    function updateButton() {
        const sel = getSelected();
        let label;
        if (sel.length === 0) {
            label = 'All';
        } else if (sel.length === 1) {
            // Show the human label, not the raw value (matters for sentinels
            // like "__all_tagged__" → "All Tagged").
            const lookup = specials.concat(options).find(o => o.value === sel[0]);
            label = lookup ? lookup.label : sel[0];
        } else if (sel.length === 2) {
            label = sel.map(v => {
                const lookup = specials.concat(options).find(o => o.value === v);
                return lookup ? lookup.label : v;
            }).join(', ');
        } else {
            label = sel.length + ' selected';
        }
        button.textContent = label;
        if (sel.length) button.classList.add('active-filter');
        else button.classList.remove('active-filter');
    }
    function updateChecks() {
        const sel = new Set(getSelected());
        popover.querySelectorAll('input[type="checkbox"]').forEach(cb => {
            cb.checked = sel.has(cb.value);
        });
    }
    function handleToggle(value) {
        const sel = new Set(getSelected());
        if (specialValues.has(value)) {
            // Sentinel: exclusive — selecting one clears the others.
            if (sel.has(value)) sel.delete(value);
            else { sel.clear(); sel.add(value); }
        } else {
            // Normal item: clears any sentinels then toggles itself.
            specialValues.forEach(s => sel.delete(s));
            if (sel.has(value)) sel.delete(value);
            else sel.add(value);
        }
        setSelected(Array.from(sel));
    }
    function buildOptions() {
        popover.innerHTML = '';
        const items = [];
        specials.forEach(s => items.push(s));
        if (specials.length && options.length) items.push({ divider: true });
        options.forEach(o => items.push(o));
        items.forEach(o => {
            if (o.divider) {
                const hr = document.createElement('div');
                hr.className = 'multi-select-divider';
                hr.setAttribute('role', 'separator');
                popover.appendChild(hr);
                return;
            }
            const item = document.createElement('label');
            item.className = 'multi-select-item';
            item.setAttribute('role', 'option');
            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.value = o.value;
            cb.addEventListener('change', () => handleToggle(o.value));
            const span = document.createElement('span');
            span.textContent = o.label;
            item.appendChild(cb);
            item.appendChild(span);
            popover.appendChild(item);
        });
        updateChecks();
    }

    function positionPopover() {
        // Use viewport coordinates (position: fixed) so the popover
        // isn't clipped by the table's horizontal-scroll container.
        // Right-align with the button so it doesn't extend off-screen
        // when the filter is in the right-most column.
        const r = button.getBoundingClientRect();
        const popW = popover.offsetWidth || 200;
        let left = r.right - popW;
        if (left < 8) left = 8; // keep on-screen on the left edge too
        popover.style.position = 'fixed';
        popover.style.top = (r.bottom + 2) + 'px';
        popover.style.left = left + 'px';
        popover.style.right = 'auto';
    }
    function open() {
        if (isOpen) return;
        isOpen = true;
        popover.style.display = 'block';
        positionPopover();
        button.setAttribute('aria-expanded', 'true');
        document.addEventListener('mousedown', onDocMouseDown);
        document.addEventListener('keydown', onDocKeyDown);
        // Reposition on scroll/resize so the popover follows the button
        // (e.g., user scrolls the page or the table horizontally with
        // the popover open).
        window.addEventListener('scroll', positionPopover, true);
        window.addEventListener('resize', positionPopover);
    }
    function close() {
        if (!isOpen) return;
        isOpen = false;
        popover.style.display = 'none';
        button.setAttribute('aria-expanded', 'false');
        document.removeEventListener('mousedown', onDocMouseDown);
        document.removeEventListener('keydown', onDocKeyDown);
        window.removeEventListener('scroll', positionPopover, true);
        window.removeEventListener('resize', positionPopover);
    }
    function onDocMouseDown(e) {
        if (!wrapper.contains(e.target) && !popover.contains(e.target)) close();
    }
    function onDocKeyDown(e) {
        if (e.key === 'Escape') { close(); button.focus(); }
    }

    button.addEventListener('click', () => isOpen ? close() : open());
    button.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowDown') {
            e.preventDefault();
            open();
            const first = popover.querySelector('input[type="checkbox"]');
            if (first) first.focus();
        }
    });

    buildOptions();
    updateButton();

    // Public setter so callers (e.g. gantt's click-to-filter shortcuts)
    // can update the widget's selection programmatically. Pass an array
    // of values; pass [] to clear. Dispatches a `change` event after.
    select._setMultiSelectValue = function (values) {
        setSelected(Array.isArray(values) ? values : [values].filter(Boolean));
    };
}

