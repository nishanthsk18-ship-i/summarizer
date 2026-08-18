import argparse
from database import generate_key

def main() -> None:
    parser = argparse.ArgumentParser(description="Manage Custom API Keys for Media Summarizer")
    parser.add_argument("--generate", action="store_true", help="Generate a new custom API key")
    parser.add_argument("--quota", type=int, default=10, help="Maximum number of video processing uses (default: 10)")
    
    args = parser.parse_args()
    
    if args.generate:
        from database import DB_PATH
        new_key = generate_key(max_quota=args.quota)
        print("\n[SUCCESS] Generated a new custom API Key!")
        print(f"Key:      {new_key}")
        print(f"Quota:    {args.quota} uses")
        print(f"Database: {DB_PATH.resolve()}\n")

    else:
        parser.print_help()

if __name__ == "__main__":
    main()
