//! Porter Stemmer (Porter 1 algorithm).
//!
//! This is a faithful Rust port of the original Porter stemming algorithm (1980),
//! matching the `porter-stemmer` npm package used in the TypeScript implementation.
//!
//! IMPORTANT: The `rust-stemmers` crate uses Snowball (Porter 2), which produces
//! DIFFERENT stems for some words. We implement Porter 1 here for parity.
//!
//! Reference: M.F. Porter, "An algorithm for suffix stripping",
//!            Program 14(3), pp 130-137, July 1980.

/// Apply the Porter stemming algorithm (Porter 1) to a word.
///
/// The word should be lowercase ASCII. Non-ASCII input is returned unchanged.
pub fn stem(word: &str) -> String {
    if word.len() <= 2 {
        return word.to_string();
    }

    // Only stem ASCII words
    if !word.bytes().all(|b| b.is_ascii_alphabetic()) {
        return word.to_string();
    }

    let mut s: Vec<u8> = word.bytes().collect();

    // Step 1a
    step1a(&mut s);
    // Step 1b
    step1b(&mut s);
    // Step 1c
    step1c(&mut s);
    // Step 2
    step2(&mut s);
    // Step 3
    step3(&mut s);
    // Step 4
    step4(&mut s);
    // Step 5a
    step5a(&mut s);
    // Step 5b
    step5b(&mut s);

    String::from_utf8(s).unwrap_or_else(|_| word.to_string())
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn is_consonant(s: &[u8], i: usize) -> bool {
    match s[i] {
        b'a' | b'e' | b'i' | b'o' | b'u' => false,
        b'y' => {
            if i == 0 {
                true
            } else {
                !is_consonant(s, i - 1)
            }
        }
        _ => true,
    }
}

/// Compute the "measure" m of the word (number of VC sequences).
fn measure(s: &[u8]) -> usize {
    let n = s.len();
    let mut i = 0;
    // Skip initial consonants
    while i < n && is_consonant(s, i) {
        i += 1;
    }
    if i >= n {
        return 0;
    }
    let mut m = 0;
    loop {
        // Skip vowels
        while i < n && !is_consonant(s, i) {
            i += 1;
        }
        if i >= n {
            return m;
        }
        m += 1;
        // Skip consonants
        while i < n && is_consonant(s, i) {
            i += 1;
        }
        if i >= n {
            return m;
        }
    }
}

/// Does the stem contain a vowel?
fn has_vowel(s: &[u8]) -> bool {
    (0..s.len()).any(|i| !is_consonant(s, i))
}

/// Does the word end with a double consonant?
fn ends_with_double_consonant(s: &[u8]) -> bool {
    let n = s.len();
    if n < 2 {
        return false;
    }
    s[n - 1] == s[n - 2] && is_consonant(s, n - 1)
}

/// Does the stem end with CVC, where the second C is not W, X, or Y?
fn ends_with_cvc(s: &[u8]) -> bool {
    let n = s.len();
    if n < 3 {
        return false;
    }
    let c2 = s[n - 1];
    if !is_consonant(s, n - 1) || is_consonant(s, n - 2) || !is_consonant(s, n - 3) {
        return false;
    }
    // The second C must not be w, x, or y
    c2 != b'w' && c2 != b'x' && c2 != b'y'
}

fn ends_with(s: &[u8], suffix: &[u8]) -> bool {
    s.len() >= suffix.len() && &s[s.len() - suffix.len()..] == suffix
}

fn replace_suffix(s: &mut Vec<u8>, suffix: &[u8], replacement: &[u8]) {
    let new_len = s.len() - suffix.len();
    s.truncate(new_len);
    s.extend_from_slice(replacement);
}

// ---------------------------------------------------------------------------
// Steps
// ---------------------------------------------------------------------------

fn step1a(s: &mut Vec<u8>) {
    if ends_with(s, b"sses") {
        replace_suffix(s, b"sses", b"ss");
    } else if ends_with(s, b"ies") {
        replace_suffix(s, b"ies", b"i");
    } else if !ends_with(s, b"ss") && ends_with(s, b"s") {
        s.pop();
    }
}

fn step1b(s: &mut Vec<u8>) {
    if ends_with(s, b"eed") {
        let stem_len = s.len() - 3;
        if measure(&s[..stem_len]) > 0 {
            replace_suffix(s, b"eed", b"ee");
        }
    } else {
        let mut found = false;
        if ends_with(s, b"ed") {
            let stem_len = s.len() - 2;
            if has_vowel(&s[..stem_len]) {
                s.truncate(stem_len);
                found = true;
            }
        } else if ends_with(s, b"ing") {
            let stem_len = s.len() - 3;
            if has_vowel(&s[..stem_len]) {
                s.truncate(stem_len);
                found = true;
            }
        }

        if found {
            if ends_with(s, b"at") {
                s.push(b'e');
            } else if ends_with(s, b"bl") {
                s.push(b'e');
            } else if ends_with(s, b"iz") {
                s.push(b'e');
            } else if ends_with_double_consonant(s)
                && !ends_with(s, b"l")
                && !ends_with(s, b"s")
                && !ends_with(s, b"z")
            {
                s.pop();
            } else if measure(s) == 1 && ends_with_cvc(s) {
                s.push(b'e');
            }
        }
    }
}

fn step1c(s: &mut Vec<u8>) {
    if ends_with(s, b"y") {
        let stem_len = s.len() - 1;
        if has_vowel(&s[..stem_len]) {
            let last = s.len() - 1;
            s[last] = b'i';
        }
    }
}

fn step2(s: &mut Vec<u8>) {
    let suffixes: &[(&[u8], &[u8])] = &[
        (b"ational", b"ate"),
        (b"tional", b"tion"),
        (b"enci", b"ence"),
        (b"anci", b"ance"),
        (b"izer", b"ize"),
        (b"abli", b"able"),
        (b"alli", b"al"),
        (b"entli", b"ent"),
        (b"eli", b"e"),
        (b"ousli", b"ous"),
        (b"ization", b"ize"),
        (b"ation", b"ate"),
        (b"ator", b"ate"),
        (b"alism", b"al"),
        (b"iveness", b"ive"),
        (b"fulness", b"ful"),
        (b"ousness", b"ous"),
        (b"aliti", b"al"),
        (b"iviti", b"ive"),
        (b"biliti", b"ble"),
    ];

    for &(suffix, replacement) in suffixes {
        if ends_with(s, suffix) {
            let stem_len = s.len() - suffix.len();
            if measure(&s[..stem_len]) > 0 {
                replace_suffix(s, suffix, replacement);
            }
            return;
        }
    }
}

fn step3(s: &mut Vec<u8>) {
    let suffixes: &[(&[u8], &[u8])] = &[
        (b"icate", b"ic"),
        (b"ative", b""),
        (b"alize", b"al"),
        (b"iciti", b"ic"),
        (b"ical", b"ic"),
        (b"ful", b""),
        (b"ness", b""),
    ];

    for &(suffix, replacement) in suffixes {
        if ends_with(s, suffix) {
            let stem_len = s.len() - suffix.len();
            if measure(&s[..stem_len]) > 0 {
                replace_suffix(s, suffix, replacement);
            }
            return;
        }
    }
}

fn step4(s: &mut Vec<u8>) {
    let suffixes: &[&[u8]] = &[
        b"al", b"ance", b"ence", b"er", b"ic", b"able", b"ible", b"ant", b"ement", b"ment",
        b"ent", b"ion", b"ou", b"ism", b"ate", b"iti", b"ous", b"ive", b"ize",
    ];

    for &suffix in suffixes {
        if ends_with(s, suffix) {
            let stem_len = s.len() - suffix.len();
            if suffix == b"ion" {
                // Special case: must end in s or t before -ion
                if stem_len > 0 && (s[stem_len - 1] == b's' || s[stem_len - 1] == b't') {
                    if measure(&s[..stem_len]) > 1 {
                        s.truncate(stem_len);
                    }
                }
            } else if measure(&s[..stem_len]) > 1 {
                s.truncate(stem_len);
            }
            return;
        }
    }
}

fn step5a(s: &mut Vec<u8>) {
    if ends_with(s, b"e") {
        let stem_len = s.len() - 1;
        let m = measure(&s[..stem_len]);
        if m > 1 || (m == 1 && !ends_with_cvc(&s[..stem_len])) {
            s.pop();
        }
    }
}

fn step5b(s: &mut Vec<u8>) {
    if measure(s) > 1 && ends_with_double_consonant(s) && ends_with(s, b"l") {
        s.pop();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_stemmer_parity() {
        let fixture: serde_json::Value = serde_json::from_str(
            include_str!("../tests/fixtures/crypto_vectors.json"),
        )
        .unwrap();

        let stemmer_tests = fixture["porter_stemmer"].as_array().unwrap();
        for test in stemmer_tests {
            let word = test["word"].as_str().unwrap();
            let expected = test["stem"].as_str().unwrap();
            let result = stem(word);
            assert_eq!(
                result, expected,
                "Porter stem('{}') = '{}', expected '{}'",
                word, result, expected
            );
        }
    }

    #[test]
    fn test_basic_stems() {
        assert_eq!(stem("caresses"), "caress");
        assert_eq!(stem("ponies"), "poni");
        assert_eq!(stem("cats"), "cat");
        assert_eq!(stem("agreed"), "agre");
        assert_eq!(stem("disabled"), "disabl");
        assert_eq!(stem("matting"), "mat");
        assert_eq!(stem("mating"), "mate");
        assert_eq!(stem("meeting"), "meet");
        assert_eq!(stem("milling"), "mill");
        assert_eq!(stem("messing"), "mess");
        assert_eq!(stem("meetings"), "meet");
    }
}
