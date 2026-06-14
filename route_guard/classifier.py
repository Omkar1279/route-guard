_LENGTH_THRESHOLD = 80


def classify(prompt: str) -> dict[str, str]:
    if len(prompt) < _LENGTH_THRESHOLD:
        return {'route': 'small', 'reason': f'Prompt under {_LENGTH_THRESHOLD} chars.'}
    return {'route': 'medium', 'reason': f'Prompt {_LENGTH_THRESHOLD}+ chars.'}
