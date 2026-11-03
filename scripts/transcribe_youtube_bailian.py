from bili_asr.cli.main import main

if __name__ == "__main__":
    import sys

    raise SystemExit(main(["transcribe", "youtube", *sys.argv[1:]]))
