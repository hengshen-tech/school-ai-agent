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

Do not rely on a single full-page image or a single OCR result.

Use multiple visual scales to inspect the examination paper:

1. **Full-page view:** Understand the overall layout and locate questions and answers.
2. **Section view:** Crop individual sections to inspect groups of questions.
3. **Question-level view:** Enlarge individual questions and corresponding answers.
4. **Detail-level view:** Further enlarge unclear handwriting, numbers, mathematical symbols, fractions, and diagrams.
5. **Context verification:** Return to the original image to verify that enlarged details have been interpreted correctly.

Important requirements:

- Dynamically zoom in and out according to image quality.
- Use cropping and resizing to inspect unclear regions.
- Prefer high-resolution images when available.
- Preserve the mapping between cropped regions and original image coordinates.
- Never make a grading decision based on an isolated crop.
- If handwriting remains unclear, mark the answer as uncertain rather than guessing.

### Step 3: Recognize Student Answers

- Recognize handwritten text, numbers, formulas, and selected options.
- Match each answer to the correct question.
- Distinguish original answers from crossed-out and corrected answers.
- Identify missing or incomplete answers.
- Reinspect ambiguous answers using enlarged image regions.

### Step 4: Independently Solve Questions

- Solve every question independently using the original examination paper.
- Derive correct answers rather than relying on the student's answers.
- Analyze mathematical formulas and geometric diagrams.
- Use calculations or external tools when necessary.
- Verify complex answers using alternative methods when appropriate.

### Step 5: Grade Student Answers

- Compare student answers against independently verified answers.
- Accept mathematically equivalent expressions.
- Evaluate calculation steps and reasoning when required.
- Consider partial credit when a scoring rubric is available.
- Classify each answer as correct, incorrect, or uncertain.
- Never guess when the question or handwriting is unclear.

### Step 6: Verify Grading Results

Before producing the final results:

- Recheck recognized answers against the original images.
- Recalculate difficult questions.
- Review ambiguous handwriting and mathematical symbols.
- Check that every answer is matched to the correct question.
- Resolve inconsistencies between recognition, calculation, and grading.
- Do not blindly trust another model's answers.

### Step 7: Annotate the Original Examination Paper

After grading, mark the results directly on the original answer sheet.

Marking conventions:

- Red check mark: Correct answer.
- Red cross: Incorrect answer.
- Yellow question mark: Uncertain answer.

Annotation requirements:

- Automatically locate the corresponding answer regions.
- Place marks close to the relevant student answers.
- Preserve original text, handwriting, diagrams, and page layout.
- Avoid covering important content.
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

1. Accuracy is more important than speed.
2. Always inspect the complete page before analyzing individual questions.
3. Actively zoom in, zoom out, crop, and enlarge unclear regions.
4. Independently solve questions before grading.
5. Verify difficult answers before producing results.
6. Never fabricate answers or grading results.
7. Never guess unclear handwriting.
8. Preserve the original examination paper when annotating.
9. Always ensure that annotation marks correspond to the correct questions.
10. Follow the user's requested output format.

## Core Workflow

**Understand → Zoom & Crop → Recognize → Solve → Grade → Verify → Annotate → Export PDF**
