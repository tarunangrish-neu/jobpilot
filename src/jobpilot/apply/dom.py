"""Generic form discovery that works across ATS page layouts.

`EXTRACT_JS` runs in the page, tags every fillable control with a
`data-jp-id` attribute (so Python can locate it again), and returns one
record per question: text inputs, textareas, selects, comboboxes, file
inputs, and radio/checkbox groups (one record per group, with options).
It only reads the DOM and sets those marker attributes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

EXTRACT_JS = r"""
() => {
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const visible = el => {
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.visibility !== 'hidden' && s.display !== 'none' && (r.width > 0 || r.height > 0);
  };
  const textOf = n => clean(n ? n.innerText || n.textContent : '');
  const hasInput = n => !!n.querySelector('input, textarea, select');

  const labelFor = el => {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && textOf(l)) return textOf(l);
    }
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const t = lb.split(/\s+/).map(i => document.getElementById(i)).filter(Boolean).map(textOf).join(' ');
      if (clean(t)) return clean(t);
    }
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    const wrap = el.closest('label');
    if (wrap && textOf(wrap)) return textOf(wrap);
    let node = el.parentElement;
    for (let i = 0; node && i < 5; i++, node = node.parentElement) {
      const cands = node.querySelectorAll('label, legend, [class*="label"], [class*="question"], [class*="title"]');
      for (const c of cands) {
        if (!c.contains(el) && !hasInput(c) && textOf(c)) return textOf(c);
      }
    }
    return clean(el.getAttribute('placeholder') || el.getAttribute('name') || '');
  };

  // Question text for a radio/checkbox group: the nearest ancestor holding
  // every option, then its first text that isn't itself an option label.
  const groupQuestion = (els) => {
    const fs = els[0].closest('fieldset');
    if (fs) {
      const lg = fs.querySelector('legend, label:not([for])');
      if (lg && textOf(lg) && !lg.contains(els[0])) return textOf(lg);
    }
    // A lone checkbox ("I am authorized to work...") is its own question; walking up
    // the DOM would grab some unrelated label.
    if (els.length === 1) return labelFor(els[0]);
    const optionLabels = new Set(els.map(labelFor));
    let node = els[0].parentElement;
    for (let i = 0; node && i < 8; i++, node = node.parentElement) {
      if (!els.every(e => node.contains(e))) continue;
      const cands = node.querySelectorAll('label, legend, [class*="label"], [class*="question"], [class*="title"], p, span, div');
      for (const c of cands) {
        const t = textOf(c);
        if (t && !hasInput(c) && !optionLabels.has(t)) return t;
      }
    }
    return els.length === 1 ? labelFor(els[0]) : '';
  };

  const isRequired = (el, label) =>
    el.required || el.getAttribute('aria-required') === 'true' || /[*\u2731]/.test(label) || /\(required\)/i.test(label);

  const out = [];
  const groups = new Map();
  let idx = 0;
  for (const el of document.querySelectorAll('input, textarea, select')) {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(type)) continue;
    if (el.closest('[aria-hidden="true"]') && type !== 'file') continue;
    if (type !== 'file' && type !== 'radio' && type !== 'checkbox' && !visible(el)) continue;
    const id = 'jp-' + (idx++);
    el.setAttribute('data-jp-id', id);

    if (type === 'radio' || type === 'checkbox') {
      // Radios share a name by definition. Checkboxes don't: Ashby names each one
      // after its option, so group checkboxes (and nameless radios) by their fieldset.
      let groupKey = type === 'radio' ? el.name : '';
      if (!groupKey) {
        const box = el.closest('fieldset, [class*="fieldEntry"], [class*="question"]');
        if (box) {
          if (!box.dataset.jpGroup) box.dataset.jpGroup = 'g' + (idx++);
          groupKey = box.dataset.jpGroup;
        } else {
          groupKey = el.name;
        }
      }
      const key = type + ':' + (groupKey || id);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(el);
      continue;
    }
    const label = labelFor(el);
    let kind = el.tagName === 'SELECT' ? 'select' : el.tagName === 'TEXTAREA' ? 'textarea' : type === 'file' ? 'file' : 'text';
    if (el.getAttribute('role') === 'combobox' && el.tagName !== 'SELECT') kind = 'combobox';
    const options = el.tagName === 'SELECT'
      ? [...el.options].map(o => clean(o.text)).filter(t => t && !/^(select|choose|please select|--)/i.test(t))
      : [];
    out.push({
      id, kind, label, options,
      name: el.getAttribute('name') || '',
      html_id: el.id || '',
      input_type: type,
      required: isRequired(el, label),
      accept: el.getAttribute('accept') || '',
    });
  }
  for (const [key, els] of groups) {
    const type = key.split(':')[0];
    const id = 'jp-' + (idx++);
    els.forEach((e, i) => e.setAttribute('data-jp-option', id + ':' + i));
    const question = groupQuestion(els);
    out.push({
      id, kind: type, label: question,
      options: els.map(labelFor),
      name: els[0].getAttribute('name') || '',
      input_type: type,
      html_id: '',
      required: els.some(e => e.required || e.getAttribute('aria-required') === 'true') || /[*\u2731]/.test(question),
      accept: '',
    });
  }
  return out;
}
"""


@dataclass
class FormField:
    id: str
    kind: str  # text | textarea | select | combobox | file | radio | checkbox
    label: str
    required: bool = False
    name: str = ""
    html_id: str = ""
    input_type: str = ""
    options: list[str] = field(default_factory=list)
    accept: str = ""

    @classmethod
    def from_js(cls, raw: dict) -> "FormField":
        return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})

    @property
    def clean_label(self) -> str:
        """Label without required markers."""
        text = self.label.replace("*", " ").replace("(required)", " ").replace("(Required)", " ")
        return " ".join(text.split())
