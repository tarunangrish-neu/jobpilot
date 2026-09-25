// Resume in the layout of the master resume PDF (Tarun_Angrish_Resume.pdf, a Google Docs
// export set in Calibri): navy capitalised name, grey headline, phone | email | links,
// navy section rules, "Title | Company, Location" with bold grey dates, justified bullets
// with the master's bold phrases, projects as "Name — text", one-line education.
//
// Data comes from the JSON file named by `--input data=/path` (path relative to the typst
// --root), written by jobpilot.tailor.render. Strings from JSON are inserted as plain text,
// never evaluated as markup.

#let data = json(sys.inputs.at("data"))
// Resumes tailored before the header fields existed still render: default what is missing.
#let contact = data.contact
#let headline = contact.at("headline", default: "")
#let plain = contact.at("plain", default: contact.at("items", default: ()))
#let links = contact.at("links", default: ())
#let bolds(e) = e.at("bold", default: e.bullets.map(_ => ""))

#let navy = rgb("#1F3864")
#let grey = rgb("#595959")
#let linkblue = rgb("#1155CC")

#set document(title: data.contact.name + " - Resume", author: data.contact.name)
#set page(paper: "us-letter", margin: (x: 0.55in, top: 0.45in, bottom: 0.45in))
// Calibri is the master's font; Carlito has identical metrics (drop its .ttf files in
// templates/fonts/); PT Sans ships with macOS and is the closest fallback.
// The master never hyphenates across lines ("ledger-based" stays whole).
#set text(font: ("Calibri", "Carlito", "PT Sans", "Helvetica Neue"), size: 10pt, lang: "en", hyphenate: false)
#set par(justify: true, leading: 0.62em, spacing: 0.7em)
#set list(marker: [•], indent: 1.1em, body-indent: 0.7em, spacing: 0.72em)

// "January 2025 - May 2026" -> "January 2025 – May 2026", as the master writes ranges.
#let range(s) = s.replace(" - ", " – ")

// Sticky: a heading never ends a page without the content under it.
#let section(title) = block(
  sticky: true,
  above: 1.05em,
  below: 0.6em,
  width: 100%,
  inset: (bottom: 2.5pt),
  stroke: (bottom: 0.9pt + navy),
  text(size: 10.5pt, weight: "bold", fill: navy, upper(title)),
)

// A bullet with its bold phrase (if the master marks one and the text still contains it).
#let emphasised(t, b) = {
  let i = if b != "" { t.position(b) } else { none }
  if i == none { t } else [#t.slice(0, i)#strong(b)#t.slice(i + b.len())]
}

#let role(title, place, dates) = block(sticky: true, above: 1.15em, below: 0.5em, grid(
  columns: (1fr, auto),
  [#text(weight: "bold", title)#if place != "" [ | #emph(place)]],
  text(weight: "bold", fill: grey, size: 9.5pt, range(dates)),
))

// --- header -------------------------------------------------------------------------
#{
  set align(center)
  set par(justify: false, spacing: 0.5em)
  block(below: 0.45em, text(size: 20pt, weight: "bold", fill: navy, upper(data.contact.name)))
  if headline != "" {
    block(below: 0.5em, text(size: 11pt, fill: grey, headline))
  }
  let parts = plain.map(p => text(p)) + links.map(l => link(l.url, underline(text(fill: linkblue, l.label))))
  block(text(size: 9.5pt, parts.join([ #h(0.25em)|#h(0.25em) ])))
}

#if data.summary != "" {
  section("Summary")
  text(data.summary)
}

#if data.experience.len() > 0 {
  section("Experience")
  for e in data.experience {
    let place = (e.company, e.location).filter(x => x != "").join(", ")
    role(e.title, place, e.dates)
    list(..e.bullets.zip(bolds(e)).map(((t, b)) => emphasised(t, b)))
  }
}

#if data.projects.len() > 0 {
  section("Projects")
  list(..data.projects.map(p => {
    let body = p.bullets.zip(bolds(p)).map(((t, b)) => emphasised(t, b)).join(" ")
    [#strong(p.name + " —") #body]
  }))
}

#if data.education.len() > 0 {
  section("Education")
  for ed in data.education {
    let place = (ed.school, ed.location).filter(x => x != "").join(", ")
    block(above: 0.75em, below: 0.75em)[
      #strong(ed.degree + ":") #place#if ed.dates != "" [ — #ed.dates]
    ]
    if ed.details.len() > 0 { list(..ed.details.map(d => text(d))) }
  }
}

#if data.skills.len() > 0 {
  section("Skills")
  for s in data.skills {
    block(above: 0.75em, below: 0.75em)[
      #strong(s.group + ":")
      // Certifications stack one per line, as on the master.
      #if lower(s.group).starts-with("certif") { s.items.join(linebreak()) } else { s.items.join(", ") }
    ]
  }
}

// Page count for the page-limit check: `typst query ... "<page-count>"`.
#context [#metadata(counter(page).final().first()) <page-count>]
