from nlistener.audio_router import AudioRouter


def main() -> None:
    router = AudioRouter()

    print("Capturable sources (route any of these to multiple outputs):")
    for source in router.list_sources():
        print(f"  [{source.index}] {source.name}  —  {source.kind} (ch={source.channels})")

    print("\nOutput devices:")
    for device in router.list_output_devices():
        print(f"  [{device.index}] {device.name} (out={device.max_output_channels})")

    if not router.has_system_audio_source():
        print("\n" + router.system_audio_setup_hint())


if __name__ == "__main__":
    main()
