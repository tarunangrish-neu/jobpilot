// One-page resume. Data comes from the JSON file named by `--input data=/path`
// (path relative to the typst --root), written by jobpilot.tailor.render.
// Strings from JSON are inserted as plain text, never evaluated as markup.

#let data = json(sys.inputs.at("data"))

#set document(title: data.contact.name + " - Resume", author: data.contact.name)
#set page(paper: "us-letter", margin: (x: 0.6in, y: 0.5in))
#set text(font: ("Helvetica Neue", "Helvetica", "Arial"), size: 10pt, lang: "en")
#set par(leading: 0.52em, spacing: 0.6em)
#set list(indent: 0.4em, body-indent: 0.45em, spacing: 0.42em)

#let section(title) = block(
  above: 0.9em,
  below: 0.5em,
  width: 100%,
  inset: (bottom: 2.5pt),
  stroke: (bottom: 0.5pt + luma(90)),
  text(size: 10.5pt, weight: "bold", tracking: 0.04em, upper(title)),
)

#let entry(left, right, sub: none, subright: none) = {
  grid(
    columns: (1fr, auto),
    row-gutter: 0.35em,
    text(weight: "bold", left), text(right),
    ..if sub != none { (text(style: "italic", sub), text(style: "italic", if subright != none { subright } else { "" })) } else { () },
  )
}

#align(center)[
  #text(size: 17pt, weight: "bold", data.contact.name)
  #v(-0.4em)
  #text(size: 9.5pt, data.contact.items.join("   |   "))
]

#if data.summary != "" {
  v(0.2em)
  text(data.summary)
}

#if data.experience.len() > 0 {
  section("Experience")
  for e in data.experience {
    entry(e.title, e.dates, sub: e.company, subright: e.location)
    list(..e.bullets.map(b => text(b)))
  }
}

#if data.projects.len() > 0 {
  section("Projects")
  for p in data.projects {
    entry(p.name, p.dates)
    list(..p.bullets.map(b => text(b)))
  }
}

#if data.skills.len() > 0 {
  section("Skills")
  for s in data.skills [
    #text(weight: "bold", s.group + ": ")#s.items.join(", ") \
  ]
}

#if data.education.len() > 0 {
  section("Education")
  for ed in data.education {
    entry(ed.degree, ed.dates, sub: ed.school, subright: ed.location)
    if ed.details.len() > 0 { list(..ed.details.map(d => text(d))) }
  }
}

// Page count for the one-page check: `typst query ... "<page-count>"`.
#context [#metadata(counter(page).final().first()) <page-count>]
