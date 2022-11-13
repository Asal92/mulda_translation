import os
import re
import json
import warnings
from enum import Enum
from dataclasses import dataclass
from typing import List, Tuple
from tqdm import tqdm
from google.cloud import translate_v2 as translate

ENCODING = 'utf-8'
# languages must be in Domain enum
SOURCE_LANGUAGE = "en"
TARGET_LANGUAGE = "fr"
INPUT_FOLDER = "data"
OUTPUT_FOLDER = "output"
INPUT_FILE_STRUCTURE = "{input_folder}/{source_language}-train.conll"
OUTPUT_FOLDER_STRUCTURE = "{output_folder}/{source_language}-{target_language}"
INPUT_FILE = INPUT_FILE_STRUCTURE.format(input_folder=INPUT_FOLDER, source_language=SOURCE_LANGUAGE)
OUTPUT_FOLDER = OUTPUT_FOLDER_STRUCTURE.format(output_folder=OUTPUT_FOLDER, source_language=SOURCE_LANGUAGE, target_language=TARGET_LANGUAGE)
JSON_FILE_NAME = "results.json"
JSON_FILE = f"{OUTPUT_FOLDER}/{JSON_FILE_NAME}"
UNTRANSLATED_CONLL_FILE_NAME = f"{TARGET_LANGUAGE}-orig-mulda.conll"
TRANSLATED_CONLL_FILE_NAME = f"{TARGET_LANGUAGE}-trans-mulda.conll"
UNTRANSLATED_CONLL_FILE = f"{OUTPUT_FOLDER}/{UNTRANSLATED_CONLL_FILE_NAME}"
TRANSLATED_CONLL_FILE = f"{OUTPUT_FOLDER}/{TRANSLATED_CONLL_FILE_NAME}"
RUN_GOOGLE_TRANSLATE = False
SECRET_JSON = "./secret/high-comfort-368404-39708e023588.json"
# how many full sentences to translate at once
# (each full sentence is translated once for each entity in the sentence plus once for the sentence as a whole)
BATCH_SIZE = 50

ID_VALUE_REGEX_KEY = "id_value"
DOMAIN_REGEX_KEY = "domain"
TOKEN_REGEX_KEY = "token"
TAG_TYPE_REGEX_KEY = "token_type"
TAG_REGEX_KEY = "tag"

START_BRACKET = '"'
END_BRACKET = '"'

TRANSLATED_TEXT_KEY = "translatedText"
INPUT_TEXT_KEY = "input"

TEMPLATE_TOKEN = "<#TEMPLATE#>"

class BracketsNotFoundWarning(UserWarning):
    '''Raised when brackets are not found in a string'''
    pass

class TranslationMismatchWarning(UserWarning):
    '''Raised when the translations of a sentence don't match each other'''
    pass

class TemplateTokenMismatchWarning(UserWarning):
    '''Raised when the number of template tokens in the original sentence and the split template don't match.
    This is usually due to punctuation being added just after the bracketing.'''
    pass

class TagCategory(Enum):
    '''The tags are:
    Location (LOC) : Facility, OtherLOC, HumanSettlement, Station
    Creative Work (CW) : VisualWork, MusicalWork, WrittenWork, ArtWork, Software, OtherCW
    Group (GRP) : MusicalGRP, PublicCORP, PrivateCORP, OtherCORP, AerospaceManufacturer, SportsGRP, CarManufacturer, TechCORP, ORG
    Person (PER) : Scientist, Artist, Athlete, Politician, Cleric, SportsManager, OtherPER
    Product (PROD) : Clothing, Vehicle, Food, Drink, OtherPROD
    Medical (MED) : Medication/Vaccine, MedicalProcedure, AnatomicalStructure, Symptom, Disease
    (Plus an empty tag for untagged words)

    The CORP tags above are incorrect and should be "Corp" instead of "CORP"
    '''
    Empty = ''
    Facility = "Facility"
    OtherLOC = "OtherLOC"
    HumanSettlement = "HumanSettlement"
    Station = "Station"
    VisualWork = "VisualWork"
    MusicalWork = "MusicalWork"
    WrittenWork = "WrittenWork"
    ArtWork = "ArtWork"
    Software = "Software"
    OtherCW = "OtherCW"
    MusicalGRP = "MusicalGRP"
    PublicCorp = "PublicCorp"
    PrivateCorp = "PrivateCorp"
    OtherCorp = "OtherCorp"
    AerospaceManufacturer = "AerospaceManufacturer"
    SportsGRP = "SportsGRP"
    CarManufacturer = "CarManufacturer"
    TechCorp = "TechCorp"
    ORG = "ORG"
    Scientist = "Scientist"
    Artist = "Artist"
    Athlete = "Athlete"
    Politician = "Politician"
    Cleric = "Cleric"
    SportsManager = "SportsManager"
    OtherPER = "OtherPER"
    Clothing = "Clothing"
    Vehicle = "Vehicle"
    Food = "Food"
    Drink = "Drink"
    OtherPROD = "OtherPROD"
    MedicationVaccine = "Medication/Vaccine"
    MedicalProcedure = "MedicalProcedure"
    AnatomicalStructure = "AnatomicalStructure"
    Symptom = "Symptom"
    Disease = "Disease"

class TagType(Enum):
    '''Tag types are either B (beginning), I (inside), or O (outside)'''
    B = "B"
    I = "I"
    O = "O"

@dataclass
class Tag:
    '''A class to hold the tag information'''
    tag_type: TagType
    tag_category: TagCategory

    def __str__(self):
        return self.tag_format

    @property
    def tag_format(self):
        return f"{self.tag_type.value}{f'-{self.tag_category.value}' if self.tag_type != TagType.O else ''}"
    

class Domain(Enum):
    '''The domains are:
    BN-Bangla
    DE-German
    EN-English
    ES-Spanish
    FA-Farsi
    FR-French
    HI-Hindi
    IT-Italian
    PT-Portuguese
    SV-Swedish
    UK-Ukrainian
    ZH-Chinese
    '''
    BN = "bn"
    DE = "de"
    EN = "en"
    ES = "es"
    FA = "fa"
    FR = "fr"
    HI = "hi"
    IT = "it"
    PT = "pt"
    SV = "sv"
    UK = "uk"
    ZH = "zh"

@dataclass
class Word:
    '''A word is a token with a tag'''
    token: str
    tag: Tag

    def __str__(self):
        return f"{self.token} <{self.tag}>"

class Sentence:
    '''A sentence is essentially a list of words together with a list of indexes of the start of each entity'''

    def __init__(self, id_value=None, domain=None, words=None, entity_indexes=None):
        if words is None:
            words = []

        if entity_indexes is None:
            entity_indexes = []

        self.id_value = id_value
        self.domain = domain
        self.words = words
        self.entity_indexes = entity_indexes

    def __str__(self):
        return " ".join([str(word) for word in self.words])

    def add_word(self, word: Word):
        self.words.append(word)
        if word.tag.tag_type == TagType.B:
            self.entity_indexes.append(len(self.words) - 1)

    def get_bracketed_sentences(self) -> List[str]:
        '''For each entity in the sentence, return a string of the sentence with the entity bracketed'''
        bracketed_sentences = []
        for entity_index in self.entity_indexes:
            bracketed_sentence = ""
            bracketing = False
            for i, word in enumerate(self.words):
                bracketed_sentence += " "
                if i == entity_index:
                    bracketed_sentence += f"{START_BRACKET}{word.token}"
                    bracketing = True
                elif bracketing:
                    if word.tag.tag_type != TagType.I:
                        assert word.tag.tag_type == TagType.O or word.tag.tag_type == TagType.B
                        # remove the trailing space
                        bracketed_sentence = bracketed_sentence[:-1]
                        bracketed_sentence += f"{END_BRACKET} {word.token}"
                        bracketing = False
                    else:
                        bracketed_sentence += word.token
                else:
                    bracketed_sentence += word.token
            if bracketing:
                # close bracket if the entity is at the end of the sentence
                bracketed_sentence += END_BRACKET
            bracketed_sentence = bracketed_sentence.strip()
            if not check_brackets(bracketed_sentence):
                raise ValueError(f"Brackets were not done correctly in {bracketed_sentence}")
            bracketed_sentences.append(bracketed_sentence)
        assert len(bracketed_sentences) == len(self.entity_indexes)
        return bracketed_sentences

    def get_entity(self, entity_indexes_index: int) -> str:
        '''Return the entity at the given index'''
        entity_index = self.entity_indexes[entity_indexes_index]
        entity = ""
        for i, word in enumerate(self.words):
            if i == entity_index:
                entity += word.token
            elif i > entity_index:
                if word.tag.tag_type != TagType.I:
                    break
                else:
                    entity += f" {word.token}"
        return entity

    def get_entities(self) -> List[str]:
        return [self.get_entity(i) for i in range(len(self.entity_indexes))]

    def get_entity_category(self, entity_indexes_index: int) -> TagCategory:
        '''Return the entity type at the given index'''
        entity_index = self.entity_indexes[entity_indexes_index]
        return self.words[entity_index].tag.tag_category

    def get_entity_categories(self) -> List[TagCategory]:
        return [self.get_entity_category(i) for i in range(len(self.entity_indexes))]

    # UNNEEDED
    # def get_all_entity_data(self) -> List[Tuple[str, TagCategory]]:
    #     return list(zip(self.get_entities(), self.get_entity_categories()))


def check_brackets(s: str) -> bool:
    if START_BRACKET == END_BRACKET:
        return s.count(START_BRACKET) == 2
    else:
        return s.count(START_BRACKET) == s.count(END_BRACKET) == 1

def get_bracket_indexes(s: str) -> Tuple[int, int]:
    assert check_brackets(s)
    start_bracket_index = s.index(START_BRACKET)
    end_bracket_index = start_bracket_index + 1 + s[start_bracket_index + 1:].index(END_BRACKET)
    return start_bracket_index, end_bracket_index

def remove_brackets(s: str) -> str:
    assert check_brackets(s)
    start_bracket_index, end_bracket_index = get_bracket_indexes(s)
    return s[:start_bracket_index] + s[start_bracket_index + 1:end_bracket_index] + s[end_bracket_index + 1:]

def remove_bracketed_entity(s: str) -> str:
    # remove everything between the two brackets
    assert check_brackets(s)
    start_bracket_index, end_bracket_index = get_bracket_indexes(s)
    return s[:start_bracket_index] + s[end_bracket_index + 1:]

def get_bracketed_entity(s: str) -> str:
    assert check_brackets(s)
    start_bracket_index, end_bracket_index = get_bracket_indexes(s)
    ret = s[start_bracket_index + 1:end_bracket_index]
    return ret

def bracket_entity(s: str, entity: str) -> str:
    # put brackets around the only instance of the entity in the string
    # raise a ValueError if the entity is not in the string or if the entity is not unique
    if s.count(entity) != 1:
        raise ValueError(f"Did not find exactly one instance of '{entity}' in '{s}'")
    return s.replace(entity, f"{START_BRACKET}{entity}{END_BRACKET}")

def list_to_generator(input_list):
    '''Convert a list to a generator'''
    for item in input_list:
        yield item

def add_conll_word(file_desc, word: Word):
    '''Add a word to the (open) file'''
    file_desc.write(f"{word.token} _ _ {word.tag.tag_format}\n")

def add_conll_id_line(file_desc, id_value: str, domain: Domain):
    '''Add an ID to the (open) file'''
    # TODO this may need fixing
    file_desc.write(f"# id {id_value}\tdomain={domain.value}\n")

if RUN_GOOGLE_TRANSLATE:
    translator = translate.Client.from_service_account_json(SECRET_JSON)

input_file = open(INPUT_FILE, 'r', encoding=ENCODING)
sentences = []
sentence = Sentence()
# from https://stackoverflow.com/a/55188797/5049813
num_lines = sum(1 for line in input_file)
input_file.seek(0)
for line in tqdm(input_file, total=num_lines):
    line = line.strip()
    if not line:
        if sentence.words:
            sentences.append(sentence)
        sentence = Sentence()
    elif line.startswith("# id"):
        # use fullmatch to get the id and domain
        # the line should look like
        # # id bb81b9a7-e73d-4977-b6a8-0f7937123dfe   domain=en
        # (note that the domain is separated out by a tab)
        match = re.fullmatch(rf"# id (?P<{ID_VALUE_REGEX_KEY}>[a-zA-Z0-9-]+)\sdomain=(?P<{DOMAIN_REGEX_KEY}>[a-z]+)", line)
        if not match:
            raise ValueError(f"The id line is not formatted correctly: {line}")
        assert sentence.id_value is None, "The id value has already been set"
        sentence.id_value = match.group(ID_VALUE_REGEX_KEY)
        sentence.domain = Domain(match.group(DOMAIN_REGEX_KEY))
    else:
        # use fullmatch to get the token, tag, and tag type
        # the line should look like
        # `tongzhi _ _ B-OtherPER`
        # or
        # `founder _ _ O`
        # (spaces here are just spaces)
        match = re.fullmatch(rf"(?P<{TOKEN_REGEX_KEY}>\S+) _ _ (?P<{TAG_TYPE_REGEX_KEY}>[BIO])(-(?P<{TAG_REGEX_KEY}>[\S]+))?", line)
        if not match:
            raise ValueError(f"The word line is not formatted correctly: {line}")
        
        token = match.group(TOKEN_REGEX_KEY)
        tag_type = TagType(match.group(TAG_TYPE_REGEX_KEY))
        tag_category = TagCategory(match.group(TAG_REGEX_KEY)) if tag_type != TagType.O else TagCategory.Empty
        tag = Tag(tag_type, tag_category)
        word = Word(token, tag)
        sentence.add_word(word)

if sentence.words:
    sentences.append(sentence)

# we now have a list of sentences
# for each entity in each sentence, we want to put the entity in brackets and translate it
sentences_to_translate: List[List[str]] = [sentence.get_bracketed_sentences() for sentence in sentences]
if RUN_GOOGLE_TRANSLATE:
    results = []
    for i in tqdm(range(len(sentences_to_translate) // BATCH_SIZE + 1)):
        start = BATCH_SIZE * i
        end = min(BATCH_SIZE * (i + 1), len(sentences_to_translate))
        # see https://stackoverflow.com/questions/1198777/double-iteration-in-list-comprehension
        batch = [sentence for sentences in sentences_to_translate[start:end] for sentence in sentences]
        results.extend(translator.translate(batch, TARGET_LANGUAGE, 'text', SOURCE_LANGUAGE))
        break # testing
    
    os.makedirs(OUTPUT_FOLDER, exist_ok=True)
    with open(JSON_FILE, 'w', encoding=ENCODING) as f:
        json.dump(results, f, indent=1)
        print(f"Results saved to {JSON_FILE}")

# we now have results in the json file
with open(JSON_FILE, 'r', encoding=ENCODING) as f:
    results = json.load(f)

# we want to put the results back into the CONLL format
trans_file = open(TRANSLATED_CONLL_FILE, 'w', encoding=ENCODING)
orig_file = open(UNTRANSLATED_CONLL_FILE, 'w', encoding=ENCODING)
translations_index = 0
db_amt = 0
max_amt = 4
for sentence_index, sentence in enumerate(tqdm(sentences)):
    number_of_entities = len(sentence.entity_indexes)
    # each one of these translations will have bracketed a single entity
    translations = [result[TRANSLATED_TEXT_KEY] for result in results[translations_index:translations_index + number_of_entities]]

    assert len(translations) == number_of_entities, f"The number of translations ({len(translations)}) does not match the number of entities ({number_of_entities})"

    translations_index += number_of_entities

    print("translations")
    print(translations)

    # the translation without any of the entities
    main_unbracketed_translation = remove_brackets(translations[0])
    skip = False
    for translation in translations:
        if not check_brackets(translation):
            warnings.warn(f"Skipping! Could not find brackets in translated sentence: {translation}", BracketsNotFoundWarning)
            skip = True
            break
        # check to make sure the translation is the same, regardless of which entity is bracketed
        unbracketed_translation = remove_brackets(translation)
        if unbracketed_translation != main_unbracketed_translation:
            warnings.warn(f"Skipping! Translated sentence '{translation}', which, when unbracketed is '{unbracketed_translation}' does not match the unbracketed main translation '{main_unbracketed_translation}'", TranslationMismatchWarning)
            skip = True
            break

    if skip:
        continue
        
    entity_categories = sentence.get_entity_categories()
    original_entity_tokens = sentence.get_entities()
    translated_entity_tokens = [get_bracketed_entity(translation) for translation in translations]

    assert len(entity_categories) == len(original_entity_tokens) == len(translated_entity_tokens), f"The number of categories ({len(entity_categories)}), original entity tokens ({len(original_entity_tokens)}), and translated entity tokens ({len(translated_entity_tokens)}) do not match"

    print("debugging up")
    print("original")
    print(original_entity_tokens)
    print("translated")
    print(translated_entity_tokens)

    # create a template for the translated sentence
    # this will be used to replace the original entity tokens with the translated entity tokens
    template = main_unbracketed_translation
    assert TEMPLATE_TOKEN not in template
    # print("Debugging")
    # print(translated_entity_tokens)
    for translated_entity_token in translated_entity_tokens:
        # this will work even if there are duplicate entities because the order remains the same
        # print(f"template: {template}")
        # print(f"entity: {translated_entity_token}")
        template = template.replace(translated_entity_token, TEMPLATE_TOKEN, 1)

    entity_indexes_index = -1
    split_template = template.split()
    if split_template.count(TEMPLATE_TOKEN) != number_of_entities:
        # this is usually due to punctuation being added just after bracketing in the translation
        warnings.warn(f"Skipping! The number of template tokens ({split_template.count(TEMPLATE_TOKEN)}) does not match the number of entities ({number_of_entities}) in the template ({template})", TemplateTokenMismatchWarning)
        continue

    # there's no more skipping at this point
    # add the id line
    example_id = f"{sentence.id_value}-{entity_indexes_index}"
    domain = Domain(TARGET_LANGUAGE)
    add_conll_id_line(trans_file, example_id, domain)
    add_conll_id_line(orig_file, example_id, domain)

    for word in split_template:
        if word == TEMPLATE_TOKEN:
            entity_indexes_index += 1
            category = entity_categories[entity_indexes_index]
            original_entity_tokens_split = original_entity_tokens[entity_indexes_index].split()
            translated_entity_tokens_split = translated_entity_tokens[entity_indexes_index].split()
            print("debugging")
            print(original_entity_tokens_split)
            print(translated_entity_tokens_split)
            for tokens, file in zip([original_entity_tokens_split, translated_entity_tokens_split], [orig_file, trans_file]):
                for token_index, token in enumerate(tokens):
                    word = Word(token, Tag(TagType.B if token_index == 0 else TagType.I, category))
                    add_conll_word(file, word)
        else:
            word = Word(word, Tag(TagType.O, TagCategory.Empty))
            add_conll_word(orig_file, word)
            add_conll_word(trans_file, word)

    db_amt += 1
    if db_amt >= max_amt:
        break
    trans_file.write("\n\n")
    orig_file.write("\n\n")

input_file.close()
trans_file.close()
orig_file.close()