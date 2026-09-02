"""Weighted multilingual keyword table for the first-pass categoriser.

Ported verbatim from the reels-workflow `classify.py` (the tuning that
produced the 1814-item hand-checked labels). `keyword -> weight`, matched
case-insensitively as a substring against a normalised blob of
caption + hashtags + transcript + on-screen text. Multi-word entries are
matched as substrings; a trailing space (``"abs "``) avoids matching
inside longer words.

Editable: the classifier eval (`pytest -m eval`) is what gates changes
here. Keys are the *default* category names — a renamed category simply
contributes no keyword score until entries are added for its new name.
"""

from __future__ import annotations

# category name -> {keyword: weight}
KEYWORDS: dict[str, dict[str, int]] = {
    "workouts": {
        "workout": 3, "exercise": 2, "gym": 3, "fitness": 3, "trening": 3,
        "cwiczenia": 3, "тренування": 3, "тренировка": 3, "reps": 2, "sets": 1,
        "squat": 2, "deadlift": 3, "bench press": 3, "biceps": 2, "triceps": 2,
        "glutes": 2, "abs ": 2, "core workout": 3, "mobility": 2, "stretching": 2,
        "calisthenics": 3, "hypertrophy": 3, "powerlifting": 3, "running": 2,
        "bieganie": 2, "cardio": 2, "hiit": 3, "yoga": 2, "pilates": 3,
        "muscle": 2, "mieśni": 2, "мышцы": 2, "posture": 1, "full body": 2,
        "push day": 3, "pull day": 3, "leg day": 3, "protein": 1, "warm up": 1,
    },
    "psychology": {
        "psychology": 3, "psychologia": 3, "психологія": 3, "психология": 3,
        "adhd": 3, "autism": 2, "autyzm": 2, "neurodivergent": 3, "anxiety": 3,
        "depression": 2, "trauma": 3, "attachment style": 3, "narcissist": 3,
        "narcyz": 3, "gaslighting": 3, "therapy": 2, "terapia": 2, "terapeuta": 2,
        "mental health": 3, "zdrowie psychiczne": 3, "self esteem": 2,
        "boundaries": 2, "granice": 1, "emotional": 1, "emocje": 2, "mindset": 2,
        "overthinking": 3, "burnout": 2, "wypalenie": 2, "coping": 2,
        "nervous system": 2, "regulation": 1, "inner child": 3, "cptsd": 3,
        "ptsd": 3, "avoidant": 2, "anxious attachment": 3, "people pleasing": 3,
        "manipulation": 2, "manipulacja": 2, "toksyczn": 2, "relationship": 1,
        "relacje": 1, "związek": 1, "стосунк": 1, "самооцінка": 3,
    },
    "books/manga": {
        "book": 2, "books": 3, "książka": 3, "książki": 3, "книга": 3, "книги": 3,
        "bookstagram": 3, "booktok": 3, "bookish": 3, "reading": 2, "read this": 1,
        "czytanie": 2, "przeczytaj": 1, "bookrecommendation": 3, "must read": 2,
        "tbr": 2, "novel": 2, "powieść": 3, "manga": 3, "manhwa": 3, "manhua": 3,
        "light novel": 3, "author": 1, "autor": 1, "literatura": 2, "literature": 2,
        "bannedbooks": 3, "bookrec": 3, "reading list": 3, "biblioteka": 1,
        "chapter": 1, "paperback": 2, "goodreads": 3, "book club": 3,
        "прочитан": 2, "книжков": 3, "literary": 2, "poetry": 2, "poezja": 2,
    },
    "movies/tv/anime": {
        "movie": 3, "film": 2, "movies": 3, "netflix": 2, "series": 2,
        "tv show": 3, "serial": 2, "anime": 3, "manhwa adaptation": 1,
        "episode": 2, "odcinek": 2, "season": 1, "sezon": 1, "trailer": 2,
        "cinema": 2, "kino": 2, "director": 2, "reżyser": 2, "scene": 1,
        "scena z filmu": 3, "hbo": 2, "disney+": 2, "studio ghibli": 3,
        "ghibli": 3, "kdrama": 3, "k-drama": 3, "cast:": 2, "runtime": 3,
        "genre:": 2, "imdb": 3, "letterboxd": 3, "sitcom": 2, "documentary": 1,
        "obejrzyj": 1, "do obejrzenia": 2, "filmzlektorem": 2, "lektor": 2,
        "shounen": 3, "shonen": 3, "otaku": 3, "crunchyroll": 3,
    },
    "memes": {
        "meme": 3, "memes": 3, "memy": 3, "mem ": 2, "śmieszne": 2, "smieszne": 2,
        "funny": 2, "humor": 1, "relatable": 2, "shitpost": 3, "brainrot": 2,
        "nawesoło": 2, "beka": 2, "hahaha": 1, "lmao": 2, "lmfao": 2, "😂": 1,
        "🤣": 1, "pov when": 1, "nobody:": 2, "me when": 1, "when you": 1,
        "прикол": 2, "мем": 3, "jokes": 1, "cursed": 2, "wysypisko_memow": 3,
        "dank": 2, "comedy skit": 1, "parody": 1,
    },
    "recipes": {
        "recipe": 3, "recipes": 3, "przepis": 3, "przepisy": 3, "рецепт": 3,
        "ingredients": 3, "składniki": 3, "інгредієнти": 3, "cooking": 2,
        "gotowanie": 2, "baking": 2, "pieczenie": 2, "dinner": 1, "obiad": 2,
        "kolacja": 1, "śniadanie": 1, "breakfast": 1, "meal prep": 2,
        "easy dinner": 2, "tablespoon": 2, "teaspoon": 2, "łyżka": 1, "łyżeczka": 1,
        "grams of": 1, "preheat": 3, "oven": 1, "piekarnik": 2, "bake for": 3,
        "dough": 2, "ciasto": 2, "sauce": 1, "sos": 1, "marinate": 2,
        "chicken breast": 2, "kurczak": 1, "foodie": 1, "yummy": 1, "pyszne": 1,
        "delicious": 1, "smacznego": 2, "kcal": 1, "high protein recipe": 3,
        "air fryer": 2, "crockpot": 2, "one pot": 2, "salad": 1, "sałatka": 1,
        "soup": 1, "zupa": 2, "dessert": 1, "deser": 2, "cukini": 1,
    },
    "lifehacks": {
        "lifehack": 3, "life hack": 3, "hack": 2, "hacks": 2, "trik": 2,
        "trick": 1, "tricks": 2, "sposób na": 2, "patent na": 2, "diy": 2,
        "tip:": 1, "pro tip": 2, "porada": 2, "лайфхак": 3, "how to clean": 2,
        "how to remove": 2, "declutter": 2, "organizing": 2, "organizacja": 1,
        "gadget": 1, "cleaning hack": 3, "storage hack": 3, "genius": 1,
        "you've been doing": 2, "did you know you can": 2, "kitchen hack": 3,
        "travel hack": 3, "money saving": 2, "oszczędzanie": 2,
    },
    "websites": {
        "website": 3, "websites": 3, "strona internetowa": 3, "app ": 2,
        "apps": 2, "aplikacja": 2, "aplikacje": 2, "free tool": 3, "tool for": 2,
        "narzędzie": 2, "narzędzia": 2, "ai tool": 3, "chatgpt": 1, "software": 1,
        "extension": 2, "wtyczka": 2, "browser extension": 3, "resource": 1,
        "free resources": 3, "these websites": 3, "5 websites": 3, "site that": 2,
        ".com that": 2, "online tool": 3, "сайт": 2, "сайти": 3,
    },
    "astrology": {
        "astrology": 3, "astrologia": 3, "астрологія": 3, "zodiac": 3,
        "zodiak": 3, "horoscope": 3, "horoskop": 3, "гороскоп": 3,
        "birth chart": 3, "natal chart": 3, "mercury retrograde": 3,
        "retrograde": 2, "full moon": 2, "new moon": 2, "pełnia księżyca": 2,
        "rising sign": 3, "ascendant": 3, "placidus": 3, "aries": 1, "taurus": 1,
        "gemini": 1, "cancer season": 2, "leo season": 2, "virgo": 1, "libra": 1,
        "scorpio": 1, "sagittarius": 1, "capricorn": 1, "aquarius": 1, "pisces": 1,
        "manifestation": 2, "manifesting": 2, "law of attraction": 3, "tarot": 3,
        "taroci": 3, "human design": 3, "numerology": 3, "spiritual": 1,
        "the universe": 1, "wszechświat": 1, "manifest": 2,
    },
    "research": {
        "study finds": 3, "research": 2, "researchers": 3, "badania": 2,
        "naukowcy": 3, "дослідження": 3, "science": 2, "nauka": 2, "наука": 2,
        "physics": 3, "fizyka": 3, "quantum": 3, "mathematics": 3, "matematyka": 3,
        "math": 2, "theorem": 3, "equation": 2, "równanie": 2, "biology": 2,
        "biologia": 2, "chemistry": 2, "chemia": 2, "neuroscience": 3,
        "astronomy": 3, "astronomia": 3, "nasa": 2, "universe expand": 2,
        "peer reviewed": 3, "hypothesis": 2, "experiment": 1, "eksperyment": 2,
        "history of": 1, "historia": 1, "engineering": 2, "inżynieria": 2,
        "evolution": 2, "ewolucja": 2, "geology": 2, "paleontolog": 3,
        "statistics": 1, "economics": 2, "ekonomia": 2, "philosophy": 2,
        "filozofia": 2, "linguistics": 3, "did you know that": 1, "fact": 1,
        "ciekawostka": 2, "dna": 1, "brain scan": 2, "psychological study": 2,
    },
    "home ideas": {
        "home decor": 3, "interior design": 3, "interiordesign": 3, "wystrój": 2,
        "wnętrza": 3, "home ideas": 3, "aranżacja": 3, "remont": 3, "renovation": 3,
        "meble": 2, "furniture": 2, "sofa": 1, "kitchen design": 3, "kuchnia": 1,
        "salon ": 1, "sypialnia": 2, "bedroom": 1, "living room": 1, "dom ": 1,
        "mieszkanie": 2, "home tour": 3, "house tour": 3, "dekoracja": 2,
        "roomtransformation": 3, "home makeover": 3, "cozy home": 2, "styling": 1,
        "ogród": 2, "garden": 1, "balkon": 2, "інтер'єр": 3, "wallpaper accent": 2,
    },
    "beauty": {
        "makeup": 3, "make-up": 3, "makijaż": 3, "skincare": 3, "pielęgnacja": 2,
        "beauty": 2, "uroda": 2, "kosmetyk": 2, "kosmetyki": 3, "cosmetics": 2,
        "retinol": 2, "serum": 2, "moisturizer": 2, "krem do twarzy": 2,
        "acne": 2, "trądzik": 2, "hairstyle": 2, "fryzura": 2, "haircare": 3,
        "włosy": 1, "nails": 2, "paznokcie": 2, "manicure": 2, "lashes": 2,
        "rzęsy": 2, "eyeliner": 2, "foundation": 1, "podkład": 2, "rossmann": 2,
        "sephora": 2, "perfume": 2, "perfumy": 2, "glow": 1, "skin purging": 3,
        "rutyna pielęgnacyjna": 3, "korean skincare": 3, "spf": 1,
    },
    "animals": {
        "cat": 2, "cats": 2, "kot": 2, "koty": 2, "kitten": 2, "kotek": 2,
        "кіт": 2, "кот": 2, "dog": 2, "pies": 2, "piesek": 2, "puppy": 2,
        "собака": 2, "catsofinstagram": 3, "dogsofinstagram": 3, "petsofinstagram": 3,
        "pet": 1, "zwierz": 2, "animal": 2, "animals": 2, "wildlife": 3,
        "тварин": 2, "raccoon": 2, "szop": 2, "fox": 1, "lis": 1, "bird": 1,
        "ptak": 1, "horse": 1, "koń": 1, "snow leopard": 3, "bigcat": 3,
        "kitty": 2, "meow": 2, "miau": 2, "paw": 1, "łapa": 1, "pupil": 1,
        "beagle": 2, "hamster": 2, "chomik": 2, "aquarium": 1, "reptile": 2,
        "zoo": 1, "przyroda": 1, "dzikiezwierz": 3, "catlover": 3,
    },
}

# A bare hashtag (no `#`) -> the category it unambiguously implies. Present
# in an item's hashtags => that category wins outright (used sparingly).
TAG_OVERRIDE: dict[str, str] = {
    "bookstagram": "books/manga", "booktok": "books/manga",
    "bookrecommendations": "books/manga", "bannedbooks": "books/manga",
    "mangarecommendations": "books/manga",
    "catsofinstagram": "animals", "dogsofinstagram": "animals",
    "catlover": "animals", "catlovers": "animals", "catlife": "animals",
    "mathmemes": "memes", "physicsmemes": "memes", "sciencememes": "memes",
    "astrology": "astrology", "zodiac": "astrology", "horoscope": "astrology",
    "skincare": "beauty", "makeup": "beauty",
    "interiordesign": "home ideas",
}
