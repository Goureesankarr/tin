# Finding course documents

These rules come from running the search by hand for a field survey tool. Queries that name
the tool mostly return paid trainings and the vendor's own academy, not courses. Queries that
describe the job in a subject's words return course documents.

## Query order

1. **The job, in course language.** `<subject> syllabus <job phrasing>`,
   `<subject> course outline lab <job phrasing>`, `"students will" <job verb> <object>`.
   Example: `agricultural extension syllabus questionnaire field survey students will collect`.
2. **Where syllabi are published.** Add the document type a region uses:
   - United States: `syllabus`, often a PDF under a department `/syllabi/` folder.
   - United Kingdom and Europe: `module handbook`, `module descriptor`, `module guide`.
   - India: `course outline`, `syllabus` with the programme and semester, e.g.
     `B.Sc. (Hons.) Agriculture syllabus semester`, often one PDF per programme.
   - Australia: `unit outline`, `unit guide`.
3. **The named alternatives.** `<alternative> syllabus`, `<alternative> lab assignment course`.
   These find `incumbent_*` and `manual` slots.
4. **The product's own name.** Finds `own` slots: courses already teaching with it.

Combine a query with `geography` when it is set. Change one term at a time so the search
log shows which term found courses.

## Multiply what works

When one course document sits in a department folder or a programme PDF, open the folder or
the programme's other semesters. One published syllabus index can hold a dozen related
courses. Note the index URL in the search log so the next run starts there.

## Shared curricula

Some documents say they follow a national or accrediting-body model curriculum. In India,
agricultural universities publish B.Sc. (Hons.) Agriculture syllabi "as per" the ICAR Deans'
Committee, with the same course codes, practicals and credits. One verified practical in such
a curriculum stands for every institution that teaches it. Record it once, name the
curriculum in the course name, and search the course title to list the institutions that
publish it. A teaching kit written for that practical works in all of them.

## Noise to drop

Do not count these as courses, but record how many each query returned:

- Paid online trainings (Udemy, Coursera, Class Central listings, commercial training
  institutes) and the vendor's own academy or certification.
- Slide decks and document-sharing copies (SlideShare, Scribd, Studocu) unless they link to
  the institution's own page.
- Blog posts, textbooks and library guides about the topic.
- AI-use and academic-integrity policies that happen to mention the job.

University short courses and continuing-education courses do count. Mark them as short
courses in the course name; their next start date is usually published.

## Reading a document

- Search operators (`site:`, `filetype:`, quoted terms) narrow results but do not guarantee
  them. A result can be returned for a word it does not contain. Only quote what you read.
- A PDF that will not open or extract is `snippet`, not a guess. A programme PDF can also
  extract only its first pages; say which pages you read.
- Check the date on the page. A short course page from years ago can still rank first.
- A page that redirects to a learning-platform sign-in is unreachable. Count it.
- A course that teaches the topic from readings and exams, with no lab or fieldwork, is
  `hands_on: false` even if the topic matches exactly.
