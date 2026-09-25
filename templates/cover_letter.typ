// Cover letter. Data from `--input data=/path` (see resume.typ):
// {contact: {name, items}, date, company, role, paragraphs: [...]}

#let data = json(sys.inputs.at("data"))

#set document(title: data.contact.name + " - Cover Letter", author: data.contact.name)
#set page(paper: "us-letter", margin: (x: 1in, y: 0.9in))
// Same font and header as the resume, so the two read as one application.
#set text(font: ("Calibri", "Carlito", "PT Sans", "Helvetica Neue"), size: 11pt, lang: "en", hyphenate: false)
#set par(leading: 0.65em, spacing: 1.1em, justify: true)

#text(size: 18pt, weight: "bold", fill: rgb("#1F3864"), upper(data.contact.name)) \
#text(size: 9.5pt, fill: rgb("#595959"), data.contact.items.join("  |  "))
#line(length: 100%, stroke: 0.9pt + rgb("#1F3864"))

#v(1.2em)
#text(data.date)

#text("Hiring Team, " + data.company) \
#text("Re: " + data.role)

#v(0.6em)
#for p in data.paragraphs [
  #text(p)

]

#text("Sincerely,") \
#text(data.contact.name)

#context [#metadata(counter(page).final().first()) <page-count>]
