---
name: exam-grading
description: >
  Automatically grade student examination papers in PDF, JPG, and PNG formats.
  Recognize handwritten answers, independently solve questions, verify grading
  results, annotate the original answer sheets, and generate graded PDF files.
---

# Exam Grading Skill

## Objective

Automatically grade student examination papers.

The primary goal is to accurately evaluate student answers and generate an annotated version of the original examination paper.

Supported input formats:
- PDF
- JPG / JPEG
- PNG

Default outputs:
1. Structured grading results in JSON.
2. An annotated PDF containing red check marks for correct answers and red crosses for incorrect answers.

## Workflow

### Step 1: Understand the Examination Paper

- Read the complete examination paper.
- Identify all questions, sub-questions, answer areas, formulas, and diagrams.
- Understand the requirements and conditions of each question.
- Identify the corresponding student answers.

### Step 2: Multi-scale Image Analysis

Start with the supplied full-page images. If the question, handwriting, diagram, and answer area are clear, recognize, solve, grade, and locate the answer directly without additional image tools.

Use additional visual scales only to resolve a specific ambiguity that affects recognition, grading, or answer location. These are optional choices, not mandatory stages for every page or question:

1. **Section view:** Prefer one crop containing related questions, their answers, and any shared diagram. Reuse this view for all relevant sub-questions.
2. **Detail view:** Enlarge only an unresolved symbol, number, or diagram detail that could change the decision.
3. **Context verification:** Check the crop against the full-page context already available. Reopen the original only if the context or coordinate mapping remains unclear.

Important requirements:

- Usually inspect an unclear region with one useful crop. Crop again only if a specific unresolved ambiguity requires a different view; avoid repeated slightly different crops of the same readable content.
- Stop using tools once the evidence is sufficient. If a focused reinspection adds no useful information, report the remaining uncertainty rather than repeatedly zooming.
- Prefer high-resolution images when available.
- Preserve the mapping between cropped regions and original image coordinates.
- Never make a grading decision based on an isolated crop.
- If handwriting remains unclear, mark the answer as uncertain rather than guessing.

### Step 3: Recognize Student Answers

- Recognize handwritten text, numbers, formulas, and selected options.
- Match each answer to the correct question.
- Distinguish original answers from crossed-out and corrected answers.
- Identify missing or incomplete answers.
- Reinspect ambiguous answers when the ambiguity could affect grading; reuse an existing useful crop whenever possible.

### Step 4: Independently Solve Questions

- Solve every question independently using the original examination paper.
- Derive correct answers rather than relying on the student's answers.
- Analyze mathematical formulas and geometric diagrams.
- Solve straightforward arithmetic and algebra directly. Use calculation tools for long or error-prone numerical work, or to resolve a concrete discrepancy; do not call a tool for every simple calculation.
- Reuse established facts and intermediate results across sub-questions sharing the same problem or diagram.
- Use an alternative method when a difficult result remains uncertain or inconsistent, rather than automatically solving every question twice.

### Step 5: Grade Student Answers

- Compare student answers against independently verified answers.
- Accept mathematically equivalent expressions.
- Evaluate calculation steps and reasoning when required.
- Consider partial credit when a scoring rubric is available.
- Classify each answer as correct, incorrect, or uncertain.
- Never guess when the question or handwriting is unclear.

### Step 6: Verify Grading Results

Before producing the final results, perform one concise consistency check using the evidence already available:

- Check question coverage, question-answer matching, verdict consistency, page numbers, and original-image coordinates.
- Reopen images or recalculate only when a specific ambiguity, contradiction, or uncertain difficult result could affect the outcome.
- Reuse confirmed recognition and calculations. Do not repeat a complete recognition, solution, or visual inspection merely to satisfy a workflow step.
- Resolve material inconsistencies; if they remain unresolved after a focused check, mark the affected answer uncertain and finish the other questions.
- Do not blindly trust another model's answers.

### Step 7: Annotate the Original Examination Paper

After grading, mark the results directly on the original answer sheet.

Marking conventions:

- Red check mark: Correct answer.
- Red cross: Incorrect answer.
- Yellow question mark: Uncertain answer.

Annotation requirements:

- Automatically locate the corresponding answer regions.
- Prefer placing marks to the right of the relevant student answers; if there is no room on the right, draw directly over the corresponding answer area.
- Preserve original text, handwriting, diagrams, and page layout.
- Keep marks associated with the correct answer area; overlaying that answer is allowed when there is no room on its right.
- Maintain the original page dimensions and image quality.
- Map annotation coordinates back to the original image.

Implementation guidance:

- Use Python Pillow (PIL) for image processing and annotation.
- Enlarge images when necessary to improve marking precision.
- Use ImageDraw to draw check marks and crosses.
- Use white outlines when needed to improve visibility.
- Resize annotated images to the appropriate original dimensions.
- Use ReportLab to combine annotated page images into a PDF.
- Preserve the original page order and generate a complete multi-page PDF when necessary.

The Agent should automatically determine annotation positions from the original examination paper rather than relying on manually predefined coordinates.

### Step 8: Generate Final Results

Default JSON format:

```json
[
  {"题号": "1", "判断": "正确"},
  {"题号": "2", "判断": "错误"},
  {"题号": "3", "判断": "正确"}
]
```

Requirements:

- Use the exact JSON structure requested by the user.
- Include sub-question numbers when necessary, such as `17(1)` and `17(2)`.
- Do not include explanations inside JSON unless requested.
- Calculate the total score when the scoring rules are available.
- Summarize incorrect and uncertain questions when requested.

Final deliverables:

1. Grading results in JSON.
2. Annotated examination paper in PDF.

## Important Principles

1. Preserve accuracy while avoiding tool calls and repeated work that add no useful evidence.
2. Always inspect the complete page before analyzing individual questions.
3. Crop and enlarge only where a specific ambiguity requires it; readable questions need no additional image tools.
4. Independently solve questions before grading.
5. Focus additional verification on uncertain or inconsistent answers; reuse results already established.
6. Never fabricate answers or grading results.
7. Never guess unclear handwriting.
8. Preserve the original examination paper when annotating.
9. Always ensure that annotation marks correspond to the correct questions.
10. Follow the user's requested output format.

## Core Workflow

**Understand → Recognize (crop only as needed) → Solve → Grade → Concise consistency check → Annotate → Export PDF**
