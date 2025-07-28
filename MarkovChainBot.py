
from typing import List, Tuple

from TwitchWebsocket import Message, TwitchWebsocket
from nltk.tokenize import sent_tokenize
import socket, time, logging, re, string

from Settings import Settings, SettingsData
from Database import Database
from Timer import LoopingTimer
from Tokenizer import detokenize, tokenize

from Log import Log
Log(__file__)

logger = logging.getLogger(__name__)

class MarkovChain:
    def __init__(self):
        self.prev_message_t = 0
        self._enabled = True
        # This regex should detect similar phrases as links as Twitch does
        self.link_regex = re.compile("\w+\.[a-z]{2,}")
        # List of moderators used in blacklist modification, includes broadcaster
        self.mod_list = []
        self.set_blacklist()
        self.learning_counter = 0
        self.generator_counter = 0
        self.awake = False
        self.learning = False
        self.learning_individuals = []
        self.learning_average = 0
        self.learning_average_peak = 0
        self.maintenance_timer = None

        # Fill previously initialised variables with data from the settings.txt file
        Settings(self)
        self.db = Database(self.chan)
        
        # Set up daemon Timer to perform maintenance tasks
        self.maintenance_timer = LoopingTimer(600, self.perform_maintenance_tasks)
        self.maintenance_timer.start()

        self.ws = TwitchWebsocket(host=self.host, 
                                  port=self.port,
                                  chan=self.chan,
                                  nick=self.nick,
                                  auth=self.auth,
                                  callback=self.message_handler,
                                  capability=["commands", "tags"],
                                  live=True)
        self.ws.start_bot()

    def set_settings(self, settings: SettingsData):
        """Fill class instance attributes based on the settings file.

        Args:
            settings (SettingsData): The settings dict with information from the settings file.
        """
        self.host = settings["Host"]
        self.port = settings["Port"]
        self.chan = settings["Channel"]
        self.nick = settings["Nickname"]
        self.auth = settings["Authentication"]
        self.denied_users = [user.lower() for user in settings["DeniedUsers"]] + [self.nick.lower()]
        self.allowed_users = [user.lower() for user in settings["AllowedUsers"]]
        self.key_length = 2
        self.max_sentence_length = settings["MaxSentenceWordAmount"]
        self.min_sentence_length = settings["MinSentenceWordAmount"]
        self.sent_separator = settings["SentenceSeparator"]
        self.emote_prefix = settings["EmotePrefix"]
        self.automatic_generation_message_count = settings["AutomaticGenerationMessageCount"]

    def message_handler(self, m: Message):
        try:
            if m.type == "366":
                logger.info(f"Successfully joined channel: #{m.channel}")
                # Get the list of mods used for modifying the blacklist
                #logger.info("Fetching mod list...")
                #self.ws.send_message("/mods")

            elif m.type == "NOTICE":
                # Check whether the NOTICE is a response to our /mods request
                if m.message.startswith("The moderators of this channel are:"):
                    string_list = m.message.replace("The moderators of this channel are:", "").strip()
                    self.mod_list = [m.channel] + string_list.split(", ")
                    logger.info(f"Fetched mod list. Found {len(self.mod_list) - 1} mods.")
                elif m.message == "There are no moderators of this channel.":
                    self.mod_list = [m.channel]
                    logger.info(f"Fetched mod list. Found no mods.")
                # If it is not, log this NOTICE
                else:
                    logger.info(m.message)

            elif m.type in ("PRIVMSG", "WHISPER"):
                if m.message.startswith("!wakeup") and self.check_if_permissions(m):
                    self.awake = True
                    logger.info("Waking up for auto-generating messages.")
                    try:
                        self.ws.send_message("NRWylder")
                    except socket.OSError as error:
                        logger.warning(f"[OSError: {error}] upon sending message. Ignoring.")
                
                elif m.message.startswith("!sleep") and self.check_if_permissions(m):
                    self.awake = False
                    logger.info("Going to sleep for auto-generating messages.")
                    try:
                        self.ws.send_message("ThankEgg")
                    except socket.OSError as error:
                        logger.warning(f"[OSError: {error}] upon sending message. Ignoring.")

            if m.type == "PRIVMSG":
                # Ignore bot messages
                if m.user.lower() in self.denied_users:
                    logger.info(f"Ignoring message. User is denied.")
                    return

                # Ignore the message if it is deemed a command
                if self.check_if_other_command(m.message):
                    logger.info(f"Ignoring message. Message is a command.")
                    return
                
                # Ignore the message if it contains a link.
                if self.check_link(m.message):
                    logger.info(f"Ignoring message. Message contained a link.")
                    return

                # Ignore if learning is paused
                if not self.learning:
                    logger.info("Ignoring message. Learning is paused.")
                    user_hash = str(hash(m.user.lower()))
                    if self.learning_individuals.count(user_hash) < 1:
                        self.learning_individuals.append(user_hash)
                    
                    if len(self.learning_individuals) >= 3:
                        self.learning = True
                        self.learning_individuals.clear()
                        logger.info("Starting learning.")
                    return

                if "emotes" in m.tags:

                    # Find emotes and remove any that do not contain the supplied emote prefix
                    # Also remove any emote that has been modified
                    emotes = m.tags["emotes"].split("/")
                    names = []
                    mods = ["_BW","_HF","_SG","_SQ","_TK"]
                    if not emotes[0] == "":
                        for e in emotes:
                            keys = e.split(":")[1].split(",")[0].split("-")
                            name = m.message[int(keys[0]):int(keys[1])+1]
                            names.append(name)

                        for n in names:
                            if self.emote_prefix == "NA":
                                m.message = m.message.replace(n, "")
                            else:
                                if not n.startswith(self.emote_prefix) or name[-3:] in mods:
                                    m.message = m.message.replace(n, "")
                                    logger.info("Stripped emote: " + n)
                    
                # Ignore the message if any word in the sentence is on the ban filter
                if self.check_filter(m.message):
                    logger.warning(f"Sentence contained blacklisted word or phrase:\"{m.message}\"")
                    return
                
                else:
                    self.generator_counter = self.generator_counter + 1
                    self.learning_counter = self.learning_counter + 1
                    
                    # Check if we should generate a message and send it to chat
                    if self.generator_counter >= self.automatic_generation_message_count:
                        self.send_activity_generation_message()

                    # Try to split up sentences. Requires nltk's 'punkt' resource
                    try:
                        sentences = sent_tokenize(m.message.strip())
                    # If 'punkt' is not downloaded, then download it, and retry
                    except:
                        logger.warning(f"Failed to tokenize {m.message}")

                    for sentence in sentences:
                        # Get all seperate words
                        words = tokenize(sentence)
                        # Double spaces will lead to invalid rules. We remove empty words here
                        if "" in words:
                            words = [word for word in words if word]
                            
                        # If the sentence is too short, ignore it and move on to the next.
                        if len(words) <= self.key_length:
                            continue
                        
                        # Add a new starting point for a sentence to the <START>
                        #self.db.add_rule(["<START>"] + [words[x] for x in range(self.key_length)])
                        self.db.add_start_queue([words[x] for x in range(self.key_length)])
                        
                        # Create Key variable which will be used as a key in the Dictionary for the grammar
                        key = list()
                        for word in words:
                            # Set up key for first use
                            if len(key) < self.key_length:
                                key.append(word)
                                continue
                            
                            self.db.add_rule_queue(key + [word])
                            
                            # Remove the first word, and add the current word,
                            # so that the key is correct for the next word.
                            key.pop(0)
                            key.append(word)
                        # Add <END> at the end of the sentence
                        self.db.add_rule_queue(key + ["<END>"])
                        # We used to increase the learning counter here, but it has been moved for now to make everything else work

            elif m.type == "CLEARMSG":
                # If a message is deleted, its contents will be unlearned
                # or rather, the "occurances" attribute of each combinations of words in the sentence
                # is reduced by 5, and deleted if the occurances is now less than 1. 
                self.db.unlearn(m.message)
                
                # TODO: Think of some efficient way to check whether it was our message that got deleted.
                # If the bot's message was deleted, log this as an error
                #if m.user.lower() == self.nick.lower():
                #    logger.error(f"This bot message was deleted: \"{m.message}\"")

            elif m.type == "RECONNECT":
                logger.info(f"Server has sent RECONNECT")

        except Exception as e:
            logger.exception(e)

    def generate(self, params: List[str] = None) -> "Tuple[str, bool]":
        """Given an input sentence, generate the remainder of the sentence using the learned data.

        Args:
            params (List[str]): A list of words to use as an input to use as the start of generating.
        
        Returns:
            Tuple[str, bool]: A tuple of a sentence as the first value, and a boolean indicating
                whether the generation succeeded as the second value.
        """
        if params is None:
            params = []

        # List of sentences that will be generated. In some cases, multiple sentences will be generated,
        # e.g. when the first sentence has less words than self.min_sentence_length.
        sentences = [[]]

        # Check for commands or recursion, eg: !generate !generate
        if len(params) > 0:
            if self.check_if_other_command(params[0]):
                return "You can't make me do commands, you madman!", False

        # Get the starting key and starting sentence.
        # If there is more than 1 param, get the last 2 as the key.
        # Note that self.key_length is fixed to 2 in this implementation
        if len(params) > 1:
            key = params[-self.key_length:]
            # Copy the entire params for the sentence
            sentences[0] = params.copy()

        elif len(params) == 1:
            # First we try to find if this word was once used as the first word in a sentence:
            key = self.db.get_next_single_start(params[0])
            if key == None:
                # If this failed, we try to find the next word in the grammar as a whole
                key = self.db.get_next_single_initial(0, params[0])
                if key == None:
                    # Return a message that this word hasn't been learned yet
                    return f"I haven't extracted \"{params[0]}\" from chat yet.", False
            # Copy this for the sentence
            sentences[0] = key.copy()

        else: # if there are no params
            # Get starting key
            key = self.db.get_start()
            if key:
                # Copy this for the sentence
                sentences[0] = key.copy()
            else:
                # If nothing's ever been said
                return "There is not enough learned information yet.", False
        
        # Counter to prevent infinite loops (i.e. constantly generating <END> while below the 
        # minimum number of words to generate)
        i = 0
        while self.sentence_length(sentences) < self.max_sentence_length and i < self.max_sentence_length * 2:
            # Use key to get next word
            if i == 0:
                # Prevent fetching <END> on the first word
                word = self.db.get_next_initial(i, key)
            else:
                word = self.db.get_next(i, key)

            i += 1

            if word == "<END>" or word == None:
                # Break, unless we are before the min_sentence_length
                if i < self.min_sentence_length:
                    key = self.db.get_start()
                    # Ensure that the key can be generated. Otherwise we still stop.
                    if key:
                        # Start a new sentence
                        sentences.append([])
                        for entry in key:
                            sentences[-1].append(entry)
                        continue
                break

            # Otherwise add the word
            sentences[-1].append(word)
            
            # Shift the key so on the next iteration it gets the next item
            key.pop(0)
            key.append(word)
        
        # If there were params, but the sentence resulting is identical to the params
        # Then the params did not result in an actual sentence
        # If so, restart without params
        if len(params) > 0 and params == sentences[0]:
            return "I haven't learned what to do with \"" + detokenize(params[-self.key_length:]) + "\" yet.", False

        return self.sent_separator.join(detokenize(sentence) for sentence in sentences), True

    def sentence_length(self, sentences: List[List[str]]) -> int:
        """Given a list of tokens representing a sentence, return the number of words in there.

        Args:
            sentences (List[List[str]]): List of lists of tokens that make up a sentence,
                where a token is a word or punctuation. For example:
                [['Hello', ',', 'you', "'re", 'Tom', '!'], ['Yes', ',', 'I', 'am', '.']]
                This would return 6.

        Returns:
            int: The number of words in the sentence.
        """
        count = 0
        for sentence in sentences:
            for token in sentence:
                if token not in string.punctuation and token[0] != "'":
                    count += 1
        return count

    def write_blacklist(self, blacklist: List[str]) -> None:
        """Write blacklist.txt given a list of banned words.

        Args:
            blacklist (List[str]): The list of banned words to write.
        """
        logger.debug("Writing Blacklist...")
        with open("blacklist.txt", "w") as f:
            f.write("\n".join(sorted(blacklist, key=lambda x: len(x), reverse=True)))
        logger.debug("Written Blacklist.")

    def set_blacklist(self) -> None:
        """Read blacklist.txt and set `self.blacklist` to the list of banned words."""
        logger.debug("Loading Blacklist...")
        try:
            with open("blacklist.txt", "r") as f:
                self.blacklist = [l.replace("\n", "") for l in f.readlines()]
                logger.debug("Loaded Blacklist.")
        
        except FileNotFoundError:
            logger.warning("Loading Blacklist Failed!")
            self.blacklist = ["<start>", "<end>"]
            self.write_blacklist(self.blacklist)

    def perform_maintenance_tasks(self) -> None:
        # Handle automatically enabling/disabling learning, as well as statistics
        # If there are no messages in the last 10 minutes we disable learning
        if self.learning_counter > 0:
            if self.learning_average == 0:
                self.learning_average = self.learning_counter
                self.learning_average_peak = self.learning_counter
            else:
                self.learning_average = round((self.learning_average + self.learning_counter) / 2)
                if self.learning_average > self.learning_average_peak:
                    self.learning_average_peak = round((self.learning_average_peak+self.learning_average)/2)
            logger.info(f"Learned from {self.learning_counter} new messages")
            logger.info(f"Learning average is {self.learning_average} and peak is {self.learning_average_peak}")
            self.learning_counter = 0
        else:
            logger.info(f"Automatically disabling message generation due to inactivity.")
            self.awake = False
            logger.info(f"Automatically disabling learning because learning counter is {self.learning_counter}")
            self.learning = False
            self.learning_average_peak = 0
            self.learing_average = 0
            self.learning_individuals.clear()
        
        # Calculate passive boosts for greater stability
        if self.learning_average > 0:
            peak_boost = 0
            time_boost = 0
            # Boost up 80% towards peak message rate
            if self.learning_average < self.learning_average_peak:
                peak_boost = round((self.learning_average_peak - self.learning_average)*0.8)

            # Boost up 80% towards one message per 30 minutes
            if self.learning_average < round((self.automatic_generation_message_count/30)*10*0.8):
                time_boost = round((((self.automatic_generation_message_count/30)*10) - self.learning_average))

            if peak_boost > time_boost:
                self.generator_counter = round(self.generator_counter + peak_boost)
            else:
                self.generator_counter = round(self.generator_counter + time_boost)

            logger.info(f"Calculated {time_boost} time boost and {peak_boost} peak boost, choosing largest.")
            logger.info(f"Chat activity counter at {self.generator_counter} out of {self.automatic_generation_message_count}")

            # Check if we should generate a message and send it to chat
            if self.generator_counter >= self.automatic_generation_message_count:
                self.send_activity_generation_message()


    def send_activity_generation_message(self) -> None:
        """Based on chat activity, send a generation message to the connected chat.
        """
        self.generator_counter = 0
        if self.awake:
            sentence, success = self.generate()
            if success:
                logger.info(sentence)
                # Try to send a message. Just log a warning on fail
                try:
                    self.ws.send_message(sentence)
                except socket.OSError as error:
                    logger.warning(f"[OSError: {error}] upon sending automatic generation message. Ignoring.")
            else:
                logger.info("Attempted to output automatic generation message, but there is not enough learned information yet.")


    def check_filter(self, message: str) -> bool:
        """Returns True if message contains a banned word.
        
        Args:
            message (str): The message to check.
        """
        for word in tokenize(message):
            if word.lower() in self.blacklist:
                return True
        return False

    def check_if_our_command(self, message: str, *commands: "Tuple[str]") -> bool:
        """True if the first "word" of the message is in the tuple of commands

        Args:
            message (str): The message to check for a command.
            commands (Tuple[str]): A tuple of commands.

        Returns:
            bool: True if the first word in message is one of the commands.
        """
        return message.split()[0] in commands

    def check_if_generate(self, message: str) -> bool:
        """True if the first "word" of the message is one of the defined generate commands.

        Args:
            message (str): The message to check for the generate command (i.e !generate or !g).
        
        Returns:
            bool: True if the first word in message is a generate command.
        """
        return self.check_if_our_command(message, *self.generate_commands)
    
    def check_if_other_command(self, message: str) -> bool:
        """True if the message is any command, except /me. 

        Is used to avoid learning and generating commands.

        Args:
            message (str): The message to check.

        Returns:
            bool: True if the message is any potential command (starts with a '!', '/' or '.')
                with the exception of /me.
        """
        return message.startswith(("!", "/", ".")) and not message.startswith("/me")
    
    def check_if_permissions(self, m: Message) -> bool:
        """True if the user has heightened permissions.
        
        E.g. permissions to bypass cooldowns, update settings, disable the bot, etc.
        True for the streamer themselves, and the users set as the allowed users.

        Args:
            m (Message): The Message object that was sent from Twitch. 
                Has `user` and `channel` attributes.
        """
        return m.user == m.channel or m.user in self.allowed_users

    def check_link(self, message: str) -> bool:
        """True if `message` contains a link.

        Args:
            message (str): The message to check for a link.

        Returns:
            bool: True if the message contains a link.
        """
        return self.link_regex.search(message)

if __name__ == "__main__":
    MarkovChain()
