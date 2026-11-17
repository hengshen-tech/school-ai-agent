"""Task-scoped library files for the single Agent to inspect and choose from."""
import json
from pathlib import Path

from . import store


class PaperLibrary:
    def __init__(self, directory):
        self.directory = Path(directory).resolve() / 'paper-library'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.papers, self.paper_paths, self.read_papers = {}, {}, set()
        self.allowed_files = set()
        catalog = []
        for item in store.list_papers():
            paper = store.get_paper(item['id'])
            if not paper or not paper.get('questions'):
                continue
            keys = {str(a['id']): a for a in paper['answers']}
            if len(keys) != len(paper['answers']) or set(keys) != {str(q['id']) for q in paper['questions']} or any(
                    not str(keys.get(str(q['id']), {}).get('answer') or '').strip() for q in paper['questions']):
                continue  # An incomplete key cannot replace a complete new solution.
            pid = paper['id']
            path = self._write(f'paper-{len(self.papers) + 1}.json', {
                'id': pid, 'rev': paper.get('rev', 0), 'title': paper['title'],
                'questions': paper['questions'],
                'answers': [{k: a[k] for k in ('id', 'answer', 'accepted', 'knowledge', 'review_status') if k in a}
                            for a in paper['answers']],
                'reference_image_paths': [r['path'] for r in paper.get('reference_pages', [])]})
            self.papers[pid] = paper
            self.paper_paths[path] = pid
            self.allowed_files.update(str(Path(r['path']).resolve()) for r in paper.get('reference_pages', [])
                                      if Path(r['path']).is_file())
            questions = paper['questions']
            samples = list(dict.fromkeys((0, len(questions) // 2, len(questions) - 1)))
            catalog.append({'id': pid, 'rev': paper.get('rev', 0), 'title': paper['title'][:120],
                            'question_count': len(questions), 'path': path,
                            'question_samples': [questions[i]['question'][:180] for i in samples]})
        paths = [self._write(f'catalog-{i // 20 + 1}.json', {'papers': catalog[i:i + 20]})
                 for i in range(0, len(catalog), 20)]
        index = self._write('index.json', {'paper_count': len(catalog), 'catalog_paths': paths})
        self.context = {'paper_count': len(catalog), 'index_path': index}

    def _write(self, name, value):
        path = self.directory / name
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), 'utf-8')
        self.allowed_files.add(str(path))
        return str(path)

    def record_read(self, path):
        pid = self.paper_paths.get(str(Path(path).resolve()))
        if pid:
            self.read_papers.add(pid)

    def resolve_selection(self, data):
        pid = data.get('selected_paper_id')
        if not pid:
            return None
        if not isinstance(pid, str) or pid not in self.papers or pid not in self.read_papers:
            raise ValueError('Agent 只能选择本次已读取的试卷库候选试卷')
        paper = self.papers[pid]
        current = store.get_paper(pid)
        rev = data.get('selected_paper_rev')
        if type(rev) is not int or rev != paper.get('rev', 0) or not current or rev != current.get('rev', 0):
            raise ValueError('Agent 选择的试卷版本已变更，请重试以读取最新答案')
        if not str(data.get('paper_match_reason') or '').strip():
            raise ValueError('Agent 选择试卷时必须说明题目对应依据')
        return current
