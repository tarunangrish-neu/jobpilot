// Cover letter. Data from `--input data=/path` (see resume.typ):
// {contact: {name, items}, date, company, role, paragraphs: [...]}

#let data = json(sys.inputs.at("data"))

#set document(title: data.contact.name + " - Cover Letter", author: data.contact.name)
#set page(paper: "us-letter", margin: (x: 1in, y: 0.9in))
#set text(font: ("Helvetica Neue", "Helvetica", "Arial"), size: 11pt, lang: "en")
#set par(leading: 0.65em, spacing: 1.1em, justify: false)

#text(size: 14pt, weight: "bold", data.contact.name) \
#text(size: 9.5pt, data.contact.items.join("   |   "))

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
