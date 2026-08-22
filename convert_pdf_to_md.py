import os
import json
import re
import time
import pypdf

BASE_DIR = os.path.dirname(os.path.realpath(__file__))
PDF_KB_DIR = os.path.join(BASE_DIR, "knowledge_base")
MD_KB_DIR = os.path.join(BASE_DIR, "knowledge_base_md")
MD_INDEX_PATH = os.path.join(BASE_DIR, "kb_md_index.json")


def sanitize_text(text: str) -> str:
    """Removes null bytes and non-printable control characters, normalizes whitespace."""
    if not text:
        return ""
    # Remove control characters except newline and tab
    clean = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    # Collapse multiple consecutive blank lines
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean.strip()


def convert_all_pdfs_to_md():
    os.makedirs(MD_KB_DIR, exist_ok=True)
    print("=" * 60)
    print("  PDF -> MARKDOWN CONVERTER & TOKEN OPTIMIZER")
    print("=" * 60)

    start_time = time.time()
    total_pages = 0
    total_raw_chars = 0
    total_md_chars = 0
    md_index = []

    pdf_files = [f for f in os.listdir(PDF_KB_DIR) if f.endswith(".pdf")]
    print(f"Found {len(pdf_files)} PDF books in knowledge_base/\n")

    for idx, pdf_name in enumerate(pdf_files, 1):
        pdf_path = os.path.join(PDF_KB_DIR, pdf_name)
        book_title = os.path.splitext(pdf_name)[0]
        md_file_name = f"{book_title}.md"
        md_file_path = os.path.join(MD_KB_DIR, md_file_name)

        print(f"[{idx}/{len(pdf_files)}] Converting '{pdf_name}'...")

        try:
            reader = pypdf.PdfReader(pdf_path)
            book_md_lines = [f"# 📖 {book_title}\n"]
            book_pages_count = 0

            for page_num, page in enumerate(reader.pages, 1):
                raw_text = page.extract_text() or ""
                total_raw_chars += len(raw_text)
                clean_text = sanitize_text(raw_text)

                if len(clean_text) > 40:
                    book_pages_count += 1
                    total_pages += 1

                    page_header = f"## [КНИГА: {book_title} | СТРАНИЦА: {page_num}]\n"
                    page_md = f"{page_header}\n{clean_text}\n"
                    book_md_lines.append(page_md)
                    total_md_chars += len(page_md)

                    md_index.append({
                        "book": pdf_name,
                        "title": book_title,
                        "page": page_num,
                        "text": clean_text
                    })

            with open(md_file_path, "w", encoding="utf-8") as f_md:
                f_md.write("\n\n".join(book_md_lines))

            print(f"   -> Processed {len(reader.pages)} pages ({book_pages_count} valid text pages). Saved to {md_file_name}")

        except Exception as e:
            print(f"   -> ERROR converting {pdf_name}: {e}")

    # Save compiled JSON Markdown Index for lightning-fast RAG searching
    print("\nSaving compiled Markdown Index to kb_md_index.json...")
    with open(MD_INDEX_PATH, "w", encoding="utf-8") as f_out:
        json.dump(md_index, f_out, ensure_ascii=False)

    duration = time.time() - start_time
    token_savings_pct = (1 - (total_md_chars / max(total_raw_chars, 1))) * 100

    print("\n" + "=" * 60)
    print("  CONVERSION COMPLETE SUCCESSFULLY!")
    print("=" * 60)
    print(f"Total Pages Processed: {total_pages}")
    print(f"Markdown Files Created: {len(pdf_files)} files in knowledge_base_md/")
    print(f"Index Saved: kb_md_index.json ({len(md_index)} page entries)")
    print(f"Execution Time: {duration:.2f} seconds")
    print(f"Estimated Token Cleanup / Compression: {token_savings_pct:.1f}% reduction")
    print("=" * 60)


if __name__ == "__main__":
    convert_all_pdfs_to_md()

