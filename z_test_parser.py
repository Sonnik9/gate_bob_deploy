# from TG.tg_parser import TgParser


# # ===== тесты =====
# TESTS = [
#     """UPD: ETH/USDT
# NEW TP: 3.289,034
# NEW SL:
# """,

#     """UPD: ETH/USDT
# NEW TP: 3.289,034
# NEW SL: 3.289,034
# """,

#     """UPD: ETH/USDT
# NEW TP: 3.289,034
# NEW SL: BE
# """,

#     """UPD: ETH/USDT
# NEW TP:
# NEW SL: 3.289,034
# """
# ]


# class DummyErr:
#     def wrap_foreign_methods(self, *_):
#         pass


# parser = TgParser(DummyErr())

# for i, msg in enumerate(TESTS, 1):
#     res, ok = parser.parse_tg_message(msg)
#     print(f"\n=== TEST {i} ===")
#     print("ok =", ok)
#     print(res)
